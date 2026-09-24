"""E2E: every message POST carries a stable_id for idempotent dispatch.

The client generates a 32-char hex ``stable_id`` at send time and
includes it in the POST body so the server can recognise a retry and
skip re-dispatching to the runner.  This test intercepts the
``/events`` POST and asserts the field is present with the right
format — a minimal guard that the wiring from ``send()``/
``enqueueMessage()`` through to the network layer is intact.

The server-side dedup (both dispatch paths answering a repeated
``stable_id`` without a second forward) is tested in
``tests/server/integration/test_sessions_endpoints.py``; the client-side
re-send of a message whose fetch threw is unit-tested in
``web/src/store/chatStore.test.ts``.  The tests here close the gap by
proving the field reaches the wire through the full SPA path, and that
a lost first POST is re-sent with the same id rather than shown as a
failure.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable

import httpx
from playwright.sync_api import Page, Route, expect

_STABLE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SEND_TEXT = "sentinel-stable-id-e2e verify this goes through"
_COMPOSER_LABEL = "Message the agent"
_RETRY_TEXT = "sentinel-retry-e2e the first post never gets an answer"
_RELOAD_TEXT = "sentinel-reload-e2e this message must survive a refresh"
_NORMAL_TEXT = "sentinel-normal-e2e a healthy send shows nothing under the bubble"
_LAG_TEXT = "sentinel-lag-e2e a slow but healthy send only shows the spinner"
_LOST_TEXT = "sentinel-lost-e2e the server got this but the reply was dropped"
_OFFLINE_TEXT = "sentinel-offline-e2e this goes out by itself when the network is back"
_CANCEL_TEXT = "sentinel-cancel-e2e this one is taken back before it ever goes out"
_REFUSED_TEXT = "sentinel-refused-e2e the runner never came up"
_GATEWAY_TEXT = "sentinel-gateway-e2e a 502 says nothing definitive"
_REFUSAL_CAUSE = (
    "The host launched runner runner_token_e2e for this session, but it never connected"
)
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_FOOTER = '[data-testid="send-delivery"]'
_FAILED_FOOTER = '[data-testid="send-delivery"][data-state="failed"]'


def _user_message_count(base_url: str, session_id: str, text: str) -> int:
    """How many committed user items in the session carry exactly ``text``."""
    rows = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=10).json()["data"]
    return sum(
        1
        for row in rows
        if row.get("type") == "message"
        and row.get("role") == "user"
        and any(
            isinstance(block, dict) and block.get("text") == text
            for block in row.get("content") or []
        )
    )


def _wait_until(predicate: Callable[[], bool], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return predicate()


def _is_message_post(route: Route) -> bool:
    if route.request.method != "POST" or "/events" not in route.request.url:
        return False
    body = json.loads(route.request.post_data or "{}")
    return body.get("type") == "message"


def _send(page: Page, text: str):  # type: ignore[no-untyped-def]
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()
    bubble = page.locator(_USER_BUBBLE).filter(has_text=text)
    expect(bubble).to_be_visible(timeout=10_000)
    return composer, bubble


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


def test_lost_first_post_is_resent_with_the_same_stable_id(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A send whose first POST gets no response is re-sent, never failed.

    The first ``POST /events`` is aborted at the network layer, which the
    browser reports as a thrown fetch ("Failed to fetch"). The client must:

    1. Keep the message in the transcript as a pending bubble.
    2. Re-POST once on its own with the *same* ``stable_id`` to learn whether
       the first request landed (the server dedupes on it).
    3. Never show it as failed when that check succeeds: no error pill, no
       "Failed" footer, and the composer is left empty.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    bodies: list[str] = []

    def _intercept(route, request):  # type: ignore[no-untyped-def]
        if f"/v1/sessions/{session_id}/events" in request.url and request.method == "POST":
            bodies.append(request.post_data or "")
            if len(bodies) == 1:
                route.abort("failed")
                return
        route.continue_()

    page.route("**/v1/sessions/*/events", _intercept)

    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible()
    composer.fill(_RETRY_TEXT)
    page.get_by_role("button", name="Send", exact=True).click()

    bubble = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
        has_text=_RETRY_TEXT
    )
    expect(bubble).to_be_visible(timeout=10_000)

    # The check re-send is automatic, about a second later; wait for the wire.
    for _ in range(50):
        if len(bodies) >= 2:
            break
        page.wait_for_timeout(200)
    assert len(bodies) >= 2, f"first POST was aborted but no re-send followed: {bodies}"
    stable_ids = {json.loads(body)["data"]["stable_id"] for body in bodies}
    assert len(stable_ids) == 1, f"re-send changed the stable_id: {stable_ids}"

    # Never surfaced as a failure: no error pill, no "Failed" footer, the text
    # stays in the transcript (once, not duplicated), and the composer was
    # left alone.
    expect(page.locator('[data-testid="error-pill"]')).to_have_count(0)
    expect(page.locator('[data-testid="send-delivery"][data-state="failed"]')).to_have_count(0)
    expect(bubble).to_have_count(1)
    expect(composer).to_have_value("")


def test_failed_send_survives_a_reload(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A send that could not reach the server comes back after a reload.

    Every ``POST /events`` is aborted at the network layer until the network
    is "back", so the send and its automatic check both fail and, after 20 s,
    the bubble reads "Failed · Retry · Cancel". After a reload the message
    must still be in the transcript, and once the network is back it must be
    re-sent with the *same* ``stable_id`` — not lost, and not duplicated.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    stable_ids: list[str] = []
    state = {"block": True}

    def _intercept(route, request):  # type: ignore[no-untyped-def]
        if f"/v1/sessions/{session_id}/events" in request.url and request.method == "POST":
            body = json.loads(request.post_data or "{}")
            if body.get("type") == "message":
                stable_ids.append(body["data"]["stable_id"])
                if state["block"]:
                    route.abort("failed")
                    return
        route.continue_()

    page.route("**/v1/sessions/*/events", _intercept)

    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible()
    composer.fill(_RELOAD_TEXT)
    page.get_by_role("button", name="Send", exact=True).click()

    bubble = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
        has_text=_RELOAD_TEXT
    )
    expect(bubble).to_be_visible(timeout=10_000)
    expect(page.locator('[data-testid="send-delivery"][data-state="failed"]')).to_be_visible(
        timeout=30_000
    )

    page.reload()
    # Still here after the reload, still failed (the network is still down):
    # the revived send is re-sent once, and that attempt is aborted too.
    expect(bubble).to_be_visible(timeout=15_000)
    expect(page.locator('[data-testid="send-delivery"][data-state="failed"]')).to_be_visible(
        timeout=15_000
    )

    # Network back: the revived send goes through with the original id.
    state["block"] = False
    page.get_by_role("button", name="Retry").click()
    expect(page.locator('[data-testid="send-delivery"]')).to_have_count(0, timeout=15_000)
    expect(bubble).to_have_count(1)
    assert len(stable_ids) >= 2, f"expected the parked send to be re-sent: {stable_ids}"
    assert len(set(stable_ids)) == 1, f"re-send changed the stable_id: {set(stable_ids)}"
    expect(page.locator('[data-testid="error-pill"]')).to_have_count(0)


def test_normal_send_shows_no_delivery_footer(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A send the server confirms quickly shows nothing under the bubble, ever.

    The footer exists only for sends that are slow or failed; a healthy
    send must look exactly as it did before this feature: one bubble,
    no spinner, no controls, no error pill, one committed copy.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    _, bubble = _send(page, _NORMAL_TEXT)

    # Past the spinner delay with nothing shown: the send confirmed in time.
    page.wait_for_timeout(6_000)
    expect(page.locator(_FOOTER)).to_have_count(0)
    expect(page.locator('[data-testid="error-pill"]')).to_have_count(0)
    expect(bubble).to_have_count(1)
    assert _wait_until(lambda: _user_message_count(base_url, session_id, _NORMAL_TEXT) == 1, 15)


def test_slow_network_shows_only_the_spinner(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A slow but healthy send shows the spinner and nothing else.

    The message POST is held at the network layer for several seconds so it
    is still in flight past the spinner delay. Elapsed time alone must never
    offer Retry or read as failed: when the response finally lands the footer
    simply goes away and the message was delivered once.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    held: list[Route] = []

    def _hold(route: Route) -> None:
        if _is_message_post(route) and not held:
            held.append(route)  # left pending on purpose; released below
            return
        route.continue_()

    page.route(f"**/v1/sessions/{session_id}/events", _hold)
    _, bubble = _send(page, _LAG_TEXT)
    footer = page.locator(_FOOTER)
    expect(footer).to_have_attribute("data-state", "sending", timeout=8_000)
    expect(page.get_by_role("button", name="Retry")).to_have_count(0)
    expect(page.locator(_FAILED_FOOTER)).to_have_count(0)
    assert held, "the message POST was not intercepted"
    held[0].continue_()  # the slow response arrives
    expect(footer).to_have_count(0, timeout=15_000)
    expect(page.locator('[data-testid="error-pill"]')).to_have_count(0)
    expect(bubble).to_have_count(1)
    assert _wait_until(lambda: _user_message_count(base_url, session_id, _LAG_TEXT) == 1, 15)


def test_lost_response_is_confirmed_by_the_check_resend(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A POST the server took but whose reply was dropped is never shown as failed.

    The first ``/events`` POST is forwarded to the server and then aborted
    on the way back, so the browser sees "Failed to fetch" for a message
    the server has. Either the stream's receipt settles the bubble first,
    or the automatic check re-send (same ``stable_id``) does and the
    server's dedup answers with the committed item. Either way the message
    is never shown as failed and exactly one copy exists server-side.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    stable_ids: list[str] = []

    def _drop_reply(route: Route) -> None:
        if _is_message_post(route):
            stable_ids.append(json.loads(route.request.post_data or "{}")["data"]["stable_id"])
            if len(stable_ids) == 1:
                route.fetch()  # the server receives and processes the message
                route.abort("failed")  # the browser never sees its answer
                return
        route.continue_()

    page.route(f"**/v1/sessions/{session_id}/events", _drop_reply)
    composer, bubble = _send(page, _LOST_TEXT)

    expect(page.locator(_FOOTER)).to_have_count(0, timeout=15_000)
    expect(page.locator(_FAILED_FOOTER)).to_have_count(0)
    assert stable_ids and len(set(stable_ids)) == 1, stable_ids
    expect(page.locator('[data-testid="error-pill"]')).to_have_count(0)
    expect(bubble).to_have_count(1)
    expect(composer).to_have_value("")
    assert _wait_until(lambda: _user_message_count(base_url, session_id, _LOST_TEXT) == 1, 15)
    page.wait_for_timeout(2_000)
    assert _user_message_count(base_url, session_id, _LOST_TEXT) == 1


def test_offline_send_recovers_on_reconnect_without_a_click(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Offline: spinner, then Failed at 20 s, then delivered by itself on reconnect.

    With the browser offline the send and its check both fail at once.
    Nothing shows for the first seconds, then the spinner, and only after
    20 s "Failed · Retry · Cancel". When the network returns the browser's
    ``online`` event re-sends once: no click, one committed copy, and the
    composer was never touched.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
    page.context.set_offline(True)
    try:
        composer, bubble = _send(page, _OFFLINE_TEXT)
        footer = page.locator(_FOOTER)
        expect(footer).to_have_attribute("data-state", "sending", timeout=8_000)
        expect(page.get_by_role("button", name="Retry")).to_have_count(0)
        expect(page.locator(_FAILED_FOOTER)).to_be_visible(timeout=25_000)
        expect(footer).to_contain_text("Failed")
        expect(page.locator('[data-testid="error-pill"]')).to_have_count(0)
        expect(composer).to_have_value("")
    finally:
        page.context.set_offline(False)

    expect(page.locator(_FOOTER)).to_have_count(0, timeout=30_000)
    expect(bubble).to_have_count(1)
    expect(page.locator('[data-testid="error-pill"]')).to_have_count(0)
    assert _wait_until(lambda: _user_message_count(base_url, session_id, _OFFLINE_TEXT) == 1, 15)


def test_cancel_drops_a_failed_send_without_posting_it(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Cancel on a failed send removes it for good: no bubble, no draft, no POST."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
    page.context.set_offline(True)
    try:
        composer, bubble = _send(page, _CANCEL_TEXT)
        expect(page.locator(_FAILED_FOOTER)).to_be_visible(timeout=25_000)
        page.get_by_role("button", name="Cancel").click()
        expect(bubble).to_have_count(0)
        expect(composer).to_have_value("")
    finally:
        page.context.set_offline(False)

    # Reconnecting must not resurrect a cancelled message.
    page.wait_for_timeout(4_000)
    expect(bubble).to_have_count(0)
    expect(page.locator(_FOOTER)).to_have_count(0)
    assert _user_message_count(base_url, session_id, _CANCEL_TEXT) == 0


def test_server_refusal_shows_its_reason_and_a_plain_5xx_waits(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A definitive refusal reads Failed at once; a bare 5xx is checked first.

    A runner-unavailable 503 is the server's final word: the footer reads
    "Failed" immediately with the server's own cause and offers Retry. A 502
    with no error code may have arrived after the message was persisted,
    so it behaves like a dropped connection: spinner, one automatic check
    re-send with the same ``stable_id``, and "Failed" only after 20 s.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    events_url = f"**/v1/sessions/{session_id}/events"

    def _refuse(route: Route) -> None:
        if _is_message_post(route):
            route.fulfill(
                status=503,
                content_type="application/json",
                body=json.dumps(
                    {
                        "error": {
                            "code": "runner_unavailable",
                            "message": _REFUSAL_CAUSE,
                        }
                    }
                ),
            )
            return
        route.continue_()

    page.route(events_url, _refuse)
    _, refused = _send(page, _REFUSED_TEXT)
    footer = page.locator(_FAILED_FOOTER)
    expect(footer).to_be_visible(timeout=5_000)
    expect(footer).to_contain_text(_REFUSAL_CAUSE)
    expect(page.get_by_role("button", name="Retry")).to_be_visible()
    page.get_by_role("button", name="Cancel").click()
    expect(refused).to_have_count(0)
    page.unroute(events_url, _refuse)

    posts: list[str] = []

    def _gateway(route: Route) -> None:
        if _is_message_post(route):
            posts.append(json.loads(route.request.post_data or "{}")["data"]["stable_id"])
            route.fulfill(status=502, content_type="text/plain", body="Bad Gateway")
            return
        route.continue_()

    page.route(events_url, _gateway)
    _, bubble = _send(page, _GATEWAY_TEXT)
    expect(page.locator(_FOOTER)).to_have_attribute("data-state", "sending", timeout=8_000)
    assert _wait_until(lambda: len(posts) >= 2, 5), posts
    assert len(set(posts)) == 1, posts
    expect(page.locator(_FAILED_FOOTER)).to_have_count(0)
    expect(page.locator(_FAILED_FOOTER)).to_be_visible(timeout=25_000)
    expect(page.locator(_FOOTER)).not_to_contain_text("Bad Gateway")
    expect(bubble).to_have_count(1)
    expect(page.locator('[data-testid="error-pill"]')).to_have_count(0)
