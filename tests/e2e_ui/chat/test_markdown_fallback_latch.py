"""E2E: a transient render fault must not latch "Could not render this markdown."

Streamdown renders code blocks through a ``React.lazy`` chunk
(``highlighted-body-*.js``). ``MarkdownErrorBoundary`` wraps every Streamdown
seam and deliberately never resets once tripped, and React.lazy caches a
rejected import forever, so a *one-time* chunk-fetch fault during a streamed
turn (network blip, proxy hiccup, deploy swapping hashed chunks) permanently
replaces the whole message with "Could not render this markdown." + raw
source. The markdown itself is valid -- a page refresh renders it fine -- but
nothing short of a manual reload recovers the message.

This test injects that transient fault: it aborts only the FIRST request for
the highlighted-body chunk, then lets every later request through. It then
drives a real streamed turn whose reply carries a fenced code block, waits for
the turn to settle, and asserts the message recovers on its own -- the
fallback clears and the code block renders without a manual page reload.
Today the boundary latches and the fallback never clears, so this fails.
"""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_FALLBACK_TEXT = "Could not render this markdown."
_CODE_BODY = '[data-streamdown="code-block-body"]'

# Every emitted highlighted-body chunk, so this keeps matching across rebuilds
# (the hashes change on every build).
_HIGHLIGHT_CHUNKS = "**/highlighted-body-*.js"

# Unique routing substring so the mock fires exactly for this turn.
_PROMPT = "Render the demo snippet for the fallback latch check."

# Fenced code block (triggers the lazy highlighted-body import) plus trailing
# prose, proving the whole message -- not just the block -- is at stake.
_REPLY = (
    "Here is the demo snippet:\n\n```ts\nconst answer = 42;\n```\n\nThat completes the demo.\n"
)


def _abort_first_chunk_request(page: Page) -> list[int]:
    """Abort only the first highlighted-body chunk request, then recover.

    A single aborted fetch is the faithful stand-in for the intermittent
    real-world trigger: one dropped/failed chunk request while the network is
    otherwise healthy. Every later request goes through untouched, so the
    fault is genuinely transient.

    :param page: Playwright page, registered before navigation.
    :returns: A single-element list tracking how many requests were aborted,
        mutable so the caller can assert the fault actually fired.
    """
    aborted = [0]

    def _handle(route: Route) -> None:
        if aborted[0] == 0:
            aborted[0] += 1
            route.abort("failed")
            return
        route.continue_()

    page.route(_HIGHLIGHT_CHUNKS, _handle)
    return aborted


@pytest.mark.compat_smoke
def test_transient_chunk_fault_does_not_latch_markdown_fallback(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A one-time chunk-fetch fault must not permanently break a valid message.

    The reported symptom: a streamed assistant message intermittently renders
    as "Could not render this markdown." and stays that way until the user
    manually refreshes the page, even though the markdown is valid. The
    correct behavior is for the settled message to recover and render its
    content once the transient fault has cleared, with no manual reload.
    """
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _REPLY}],
        key="fallback-latch",
        match=_PROMPT,
    )

    aborted = _abort_first_chunk_request(page)

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(_PROMPT)
    page.get_by_role("button", name="Send", exact=True).click()

    # Turn settles: the assistant bubble rendered and the working shimmer is
    # gone, so the response is complete -- any fallback still on screen from
    # here on is the latch, not a mid-stream transient.
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    # The transient fault must actually have fired; otherwise the test is
    # vacuous (e.g. the chunk stopped being lazy-loaded) and needs updating.
    deadline = time.monotonic() + 10
    while aborted[0] == 0 and time.monotonic() < deadline:
        page.wait_for_timeout(250)
    assert aborted[0] >= 1, "the highlighted-body chunk request was never intercepted"

    # The bug: the one-time fault latches MarkdownErrorBoundary, so the
    # settled, valid message stays stuck on the fallback forever. Correct
    # behavior: with the network healthy again, the message recovers on its
    # own -- the fallback clears and the code block renders -- without a
    # manual page reload.
    expect(page.get_by_text(_FALLBACK_TEXT)).to_have_count(0, timeout=20_000)
    expect(page.locator(_CODE_BODY).first).to_be_visible(timeout=20_000)
    expect(page.get_by_text("That completes the demo.")).to_be_visible()
