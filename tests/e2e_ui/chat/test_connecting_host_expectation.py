"""E2E: the in-thread "Connecting host…" launch band explains the wait.

During a managed-sandbox launch the chat used to show a bare
"Connecting host…" spinner with no indication of what it means, how
long to expect, or whether it's stuck. The team expects this stage to
be quick (seconds), so the band should set that expectation the way
the hero (empty-transcript) variant already does with its description
line.

The journey: start a managed-sandbox session with a first message (the
create-then-send path renders the user's bubble immediately, so the
launch cue is the in-thread ``row`` variant), then stare at the launch
band while the sandbox pipeline is in its ``starting`` stage. The band
must carry some duration-expectation copy beyond the bare stage label;
a bare "Connecting host…" is the reported papercut.

The spawned e2e server has no managed-sandbox provider, so the browser's
``GET /v1/sessions/{id}`` is route-patched to carry the same
``sandbox_status`` payload a real managed launch publishes (the server
emits exactly this shape and stage sequence — verified server-side in
``tests/server/integration/test_host_session_binding.py``). Only the
browser-visible snapshot changes; the session, server, and SPA are real.
This mirrors the established patch pattern in
``tests/e2e_ui/chat/test_claude_model_picker.py``.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from omnigent.entities import MessageData, NewConversationItem
from tests.e2e_ui.conftest import fetch_with_retry, seed_committed_items

# Any reasonable duration-expectation wording satisfies the affordance:
# the hero variant's existing copy ("this can take a minute") matches, and
# so would "usually under 5 seconds" / "should be quick" phrasings.
_EXPECTATION_COPY = re.compile(r"second|minute|moment|quick|usually|shortly", re.IGNORECASE)


def _patch_session_sandbox_starting(page: Page, session_id: str) -> None:
    """Shape the browser's session snapshot like a launch mid-``starting``.

    Patches only ``GET /v1/sessions/{session_id}`` as seen by the browser,
    adding the in-flight ``sandbox_status`` a real managed-sandbox launch
    carries while the in-sandbox host boots and dials back (the stage the
    SPA labels "Connecting host…").

    :param page: Playwright page, before navigation.
    :param session_id: Session whose snapshot to patch.
    """

    def _handle(route: Route) -> None:
        request = route.request
        parsed = urlparse(request.url)
        if parsed.path != f"/v1/sessions/{session_id}" or request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["sandbox_status"] = {"stage": "starting", "error": None}
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)


def test_connecting_host_band_sets_a_duration_expectation(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The row-variant launch band tells the user what to expect.

    Fails when the band renders only "Connecting host…" with no
    expectation copy — the reported confusion ("no indication of what
    it means, how long to expect, or whether it's stuck").

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real
        server-backed session; the browser snapshot is patched to a
        managed-sandbox launch in the ``starting`` stage.
    """
    base_url, session_id = seeded_session

    # Create-then-send path: the user's first message is already in the
    # transcript, so the launch cue renders as the in-thread "row"
    # variant beneath it (the hero empty state never shows here).
    seed_committed_items(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_connecting_host",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "Set up my project"}],
                ),
            )
        ],
    )
    _patch_session_sandbox_starting(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    indicator = page.get_by_test_id("runner-starting-indicator")
    expect(indicator).to_be_visible(timeout=15_000)
    expect(indicator).to_contain_text("Connecting host")

    # Hold the state briefly so a recording shows exactly what the user
    # stares at during the wait.
    page.wait_for_timeout(3_000)

    # THE BUG: the band must set a duration expectation (the stage is
    # expected to take seconds), not just name an internal pipeline stage.
    band_text = indicator.inner_text()
    assert _EXPECTATION_COPY.search(band_text), (
        "The in-thread 'Connecting host…' launch band gives no indication of "
        "how long the wait should be or that it is expected to be quick — "
        f"it reads only {band_text!r}."
    )
