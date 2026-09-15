"""Browser e2e: forking a LONG session must not stall on the full transcript.

Reported journey ("make forking in the server faster"): open a long-running
session in the web UI, fork it (the per-message "Fork from here" action on
the last assistant response -> Clone), and the dialog blocks for a very long
time -- minutes on a deployed server -- before the clone opens.

What the user is waiting on is ``POST /v1/sessions/{id}/fork``. Today that
request:

1. deep-copies every source item synchronously inside the request, and
2. serializes the ENTIRE copied transcript into the 201 response body
   (``list_items(limit=10000)``), which ``ForkSessionDialog.handleFork``
   awaits before navigating -- and then uses nothing but the clone's id.

Both costs grow linearly and unboundedly with history size, so a large
session (tens of MB of items) turns the fork click into a minutes-long stall
on a deployed server: the copy pays remote-DB round-trips, and the full
transcript then travels through the app proxy and the user's WAN link into
the browser, only to be discarded.

This test drives the real journey against a seeded ~30MB / 2000-item session
and pins the size of the response the dialog blocks on: the fork must not
ship the full transcript. It FAILS while the bug is live (the body is the
whole ~30MB history) and passes once the fork response stops scaling with
source history (e.g. metadata plus at most one item page, like
``GET /v1/sessions/{id}`` which returns ~1.5MB for the same session).
"""

from __future__ import annotations

import re
import time

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_items

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# ~15KB of wrappable text per item; 2000 items ~= 30MB of history -- the
# shape of a long-lived real session (big tool outputs, long replies).
_ITEM_TEXT = ("lorem ipsum dolor sit amet consectetur adipiscing elit " * 300)[:15000]
_N_ITEMS = 2000

# The response the fork dialog blocks on must stay bounded regardless of
# source size: metadata plus at most one item page. The paged session
# snapshot for this same source is ~1.5MB; the full transcript is ~30MB.
_MAX_FORK_RESPONSE_BYTES = 5_000_000


def _seed_long_history(session_id: str) -> None:
    """Write a 2000-item / ~30MB committed transcript straight into the store.

    :param session_id: Session to append to, e.g. ``"conv_abc123"``.
    """
    from omnigent.entities import MessageData, NewConversationItem

    items = []
    for i in range(_N_ITEMS):
        role = "user" if i % 2 == 0 else "assistant"
        items.append(
            NewConversationItem(
                type="message",
                response_id=f"resp_{i // 2}",
                data=MessageData(
                    role=role,
                    content=[
                        {
                            "type": "input_text" if role == "user" else "output_text",
                            "text": f"turn {i}: {_ITEM_TEXT}",
                        }
                    ],
                    agent="hello_world" if role == "assistant" else None,
                ),
            )
        )
    for start in range(0, len(items), 250):
        seed_committed_items(session_id, items[start : start + 250])


def test_fork_large_session_response_is_not_full_transcript(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Fork a ~30MB session from the UI -- the blocking response must be small.

    Journey: open the long session -> "Fork from here" on the last assistant
    response (a full clone) -> Clone. The dialog awaits the fork response
    before it navigates, so that response's size is exactly what the user
    waits on (after the server's own synchronous full-history copy).

    Failure mode this catches: the fork response carries the entire copied
    transcript (~30MB here), so fork latency grows linearly with history --
    the "forking takes minutes" stall on deployed servers, where the same
    payload also crosses the app proxy and the user's WAN link.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    _seed_long_history(session_id)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible(timeout=60_000)

    last_assistant = page.locator(_ASSISTANT).last
    expect(last_assistant).to_be_visible(timeout=60_000)

    # ── Open the fork dialog from the last assistant response ─────────
    last_assistant.hover()
    last_assistant.get_by_test_id("fork-from-response").click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()
    submit = page.get_by_test_id("fork-session-submit")

    # ── Submit and capture the response the dialog blocks on ──────────
    started = time.monotonic()
    with page.expect_response(
        lambda r: r.url.endswith(f"/v1/sessions/{session_id}/fork"),
        timeout=300_000,
    ) as resp_info:
        submit.click()
    response = resp_info.value
    # The dialog awaits the whole body, so include the download in the wait.
    response.finished()
    elapsed = time.monotonic() - started
    # Body size via the network event, not Response.body(): a full-transcript
    # response is large enough to be evicted from the inspector cache.
    body_bytes = response.request.sizes()["responseBodySize"]
    if body_bytes <= 0:
        body_bytes = int(response.headers.get("content-length", "0"))

    assert response.status == 201, f"fork failed: HTTP {response.status}"

    # The journey itself completes: the dialog navigates into the clone.
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})(conv_)?[0-9a-f]+"),
        timeout=120_000,
    )

    # Evidence for the log: how long the user stared at the blocked dialog.
    print(
        f"fork of {_N_ITEMS}-item (~30MB) session: dialog blocked "
        f"{elapsed:.2f}s; blocking response body = {body_bytes / 1e6:.1f}MB"
    )

    # Regression pin: the user-blocking fork response must not ship the
    # full transcript. While the bug is live this is the whole ~30MB
    # history; a fixed fork returns bounded metadata (+ at most one page).
    assert body_bytes < _MAX_FORK_RESPONSE_BYTES, (
        f"fork response carried {body_bytes / 1e6:.1f}MB for a ~30MB source "
        f"-- the full copied transcript rides the response the fork dialog "
        f"blocks on, so fork latency scales with history size "
        f"(expected < {_MAX_FORK_RESPONSE_BYTES / 1e6:.0f}MB)"
    )
