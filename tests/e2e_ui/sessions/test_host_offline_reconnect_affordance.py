"""E2E: a host-offline session must show a legible reconnect affordance.

The journey: a user hosts sessions from their machine (`omnigent
host --server <url>`), the host connection drops while the session tab is
open, and the composer greys out with "Session offline — reconnect below to
continue".  "Below" the composer, nothing legible offers a reconnect: the
"Host is offline — click to reconnect" pill is suppressed whenever the
composer is on screen (ChatIndicators renders null for ``host_offline``),
and the composer's host badge shows only the host label — its "offline —
click to reconnect" wording lives in a screen-reader-only span and the
hover title.  Users read the placeholder, look below, find nothing to
click, and conclude the app is a dead end (the reported "cryptic
'Reconnect below?' message with no link").

Regression contract (what a fix must make true): while the composer
directs the user to "reconnect below", at least one LEGIBLE element (a
real, human-visible run of text, not sr-only/hover-only) must offer the
reconnect action.  Either un-suppressing the pill for ``host_offline`` or
giving the host badge visible reconnect wording satisfies it.

The offline state is staged the way the suite already does it
(``test_subagent_stale_runner_composer.py``): the browser's view of the
session is patched via route interception — the snapshot/list rows carry a
host binding and an aged ``created_at`` (past the startup grace), and
``/health`` reports the host tunnel down — the same signals a live server
emits when a host drops.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

# Aged far past STARTING_GRACE_S so the dead host reads as a drop, not a boot.
_OLD_CREATED_AT = 1_700_000_000

_HOST_ID = "host-offline-affordance-e2e"

_OFFLINE_PLACEHOLDER = "Session offline — reconnect below to continue"

# The reconnect wording the user must be able to SEE (the pill and the badge
# both phrase it this way).
_RECONNECT_TEXT = re.compile(r"click to reconnect", re.IGNORECASE)

# Minimum box for "legible": far above the 1x1px sr-only clip rect, far below
# any real inline text run.
_MIN_LEGIBLE_PX = 8.0


def _force_host_offline(page: Page, session_id: str) -> None:
    """Patch the browser's view of ``session_id`` into ``host_offline``.

    Grafts a non-resumable host binding onto the session (snapshot + sidebar
    list rows), ages it past the startup grace, and reports the host tunnel
    down via ``/health`` — truth-table row 3'' of ``useSessionLiveness``.

    :param page: Playwright page, before navigation.
    :param session_id: Session id to patch.
    """

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["created_at"] = _OLD_CREATED_AT
        payload["host_id"] = _HOST_ID
        payload["host_resumable"] = False
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_list(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/v1/sessions":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        for row in payload.get("data", []):
            if row.get("id") == session_id:
                row["created_at"] = _OLD_CREATED_AT
                row["host_id"] = _HOST_ID
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_health(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/health":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        offline = {"runner_online": False, "host_online": False}
        if isinstance(payload.get("sessions"), dict):
            payload["sessions"][session_id] = offline
        if isinstance(payload.get("session"), dict):
            payload["session"] = {**payload["session"], **offline}
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route(re.compile(r"/v1/sessions(\?|$)"), _patch_list)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)


def _legible_reconnect_affordances(page: Page) -> list[str]:
    """Return descriptions of visibly legible "click to reconnect" texts.

    Playwright counts screen-reader-only spans (1x1px clip rects) as
    "visible", so visibility alone can't tell a legible affordance from the
    badge's sr-only suffix — require a real bounding box.

    :param page: The session page in the offline state.
    :returns: One human-readable entry per legible match; empty when the
        reconnect wording exists only in sr-only/hover-only form.
    """
    legible: list[str] = []
    for element in page.get_by_text(_RECONNECT_TEXT).all():
        box = element.bounding_box()
        if box and box["width"] >= _MIN_LEGIBLE_PX and box["height"] >= _MIN_LEGIBLE_PX:
            legible.append(f"{element.evaluate('el => el.outerHTML').strip()[:120]} box={box}")
    return legible


@pytest.mark.e2e_ui
def test_host_offline_session_offers_legible_reconnect(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A host-offline session's "reconnect below" must point at something.

    Reproduces the reported web-side symptom: the composer says "Session
    offline — reconnect below to continue" while no legible element below
    (or anywhere) offers the reconnect action — the pill is suppressed and
    the badge's reconnect wording is sr-only/hover-only.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real session.
    :returns: None.
    """
    base_url, session_id = seeded_session

    _force_host_offline(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=15_000)

    # State guard — the reported situation is on screen: the session is
    # unreachable and the composer directs the user to "reconnect below".
    expect(composer).to_be_disabled(timeout=15_000)
    expect(composer).to_have_attribute("placeholder", _OFFLINE_PLACEHOLDER, timeout=15_000)

    # The host badge (the affordance the placeholder alludes to) is on
    # screen — the failure is that nothing SAYS it reconnects.
    expect(page.get_by_test_id("host-badge")).to_be_visible(timeout=10_000)

    # THE BUG: no legible reconnect affordance anywhere on the page. The
    # sr-only span inside the badge and the hover-only title do not count —
    # a user who can't see the wording can't find the action.
    legible = _legible_reconnect_affordances(page)
    assert legible, (
        "Composer says 'reconnect below to continue' but no legible element "
        "offers the reconnect action: the 'Host is offline — click to "
        "reconnect' pill is suppressed while the composer is on screen, and "
        "the host badge's reconnect wording is screen-reader-only. A user "
        "who lost the host connection has no visible way back."
    )
