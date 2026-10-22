"""E2E: every message POST carries a stable_id for idempotent dispatch.

The client generates a 32-char hex ``stable_id`` at send time and
includes it in the POST body so the server can recognise a retry and
skip re-dispatching to the runner.  This test intercepts the
``/events`` POST and asserts the field is present with the right
format — a minimal guard that the wiring from ``send()``/
``enqueueMessage()`` through to the network layer is intact.

The server-side dedup (``pending_inputs.record()`` returning an existing
entry for a matching ``stable_id``) is unit-tested in
``tests/runtime/test_pending_inputs.py``; the client-side retry
preservation (``failedSendDraft.stableId`` → ``pendingRetryStableId``)
is unit-tested in ``tests/store/chatStore.test.ts``.  This e2e test
closes the gap by proving the field reaches the wire through the full
SPA path.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, expect

_STABLE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SEND_TEXT = "sentinel-stable-id-e2e verify this goes through"
_COMPOSER_LABEL = "Message the agent"


def test_message_post_carries_stable_id(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The events POST body includes a well-formed stable_id.

    Intercepts the first ``POST /v1/sessions/.../events`` call triggered
    by a user send and asserts:

    1. ``data.stable_id`` is present in the JSON body.
    2. It matches the 32-char lowercase hex format the server expects.

    A missing or malformed ``stable_id`` means the server-side
    idempotency check never fires, so a client POST retry would
    re-dispatch to the runner and create a duplicate turn.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    captured: list[str] = []

    def _intercept(route, request):  # type: ignore[no-untyped-def]
        if (
            f"/v1/sessions/{session_id}/events" in request.url
            and request.method == "POST"
            and not captured
        ):
            captured.append(request.post_data or "")
        route.continue_()

    page.route("**/v1/sessions/*/events", _intercept)

    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible()
    composer.fill(_SEND_TEXT)
    page.get_by_role("button", name="Send", exact=True).click()

    # Optimistic bubble confirms the send reached the client-side path.
    expect(
        page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
            has_text=_SEND_TEXT
        )
    ).to_be_visible(timeout=10_000)

    assert captured, "No POST to /events was intercepted — send did not fire"
    body = json.loads(captured[0])
    stable_id = body.get("data", {}).get("stable_id")
    assert stable_id is not None, f"stable_id missing from POST body: {body}"
    assert _STABLE_ID_RE.match(stable_id), (
        f"stable_id {stable_id!r} is not a 32-char lowercase hex string"
    )
