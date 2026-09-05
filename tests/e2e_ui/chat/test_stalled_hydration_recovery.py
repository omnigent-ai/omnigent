"""The chat page must recover when its hydration snapshot stalls.

Reported symptom: the web UI sits forever showing "Loading conversation…"
after a session has already finished producing its output (e.g. a native/CLI
harness completed the turn). Opening or reloading the same session in the
browser never leaves the loading state.

Journey (web surface):

1. Open a session and drive one turn to completion, so the session has
   committed, finished output (the "already completed output" precondition).
2. Reload the page (the SPA re-hydrates from the server, exactly as a fresh
   browser open of a completed session does).
3. While the reload's hydration snapshot request
   (``GET /v1/sessions/{id}?refresh_state=true``) is held open — the state a
   momentarily slow / unresponsive runner-backed snapshot build produces — the
   page shows the ``HydratingPlaceholder`` ("Loading conversation…").

The fault being guarded: the chat page is gated on ``chatStore.bindStream``'s
hydration via ``loadingConversation``, and the ``refresh_state=true`` snapshot
re-polls the runner, so a stalled response used to leave the page wedged on
the placeholder forever — no transcript, no composer, no error/retry. The
request must *hang*, not fail: an aborted/errored snapshot rejects into
``conversationLoadError`` (a graceful error screen), so only a hung response
exercises the loading gate.

This test asserts the page RECOVERS from the loading gate within a bounded
window (composer usable again, or the loading placeholder otherwise cleared).
On a build with no hydration bound the assertion times out; a build that
bounds the snapshot attempt (falling back to the cheap DB-only read, or
surfacing an error+retry) passes.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_LOADING_TEXT = "Loading conversation…"

# A wedged build stays on "Loading conversation…" indefinitely (no bound on
# the hydration snapshot). A generous-but-bounded recovery window: far longer
# than any legitimate hydration (including the client's stall-rescue window),
# short enough to keep the test fast. A recovered build clears the
# placeholder well inside it.
_RECOVER_TIMEOUT_MS = 20_000


def _stall_hydration_snapshot(page: Page, session_id: str) -> list[int]:
    """Hold the reload's hydration snapshot request open (never respond).

    Intercepts only ``GET /v1/sessions/{id}?...refresh_state=true`` — the
    runner-backed read ``bindStream`` awaits to clear ``loadingConversation``.
    Everything else (the cheap DB-only snapshot, the items page, the SSE
    ``/stream``, health polls) is let through so the ONLY thing stalled is the
    runner-backed hydration leg, isolating the "stuck on Loading" defect.

    A matched request is deliberately left unhandled so it stays pending,
    simulating a runner-backed snapshot build that hangs rather than errors.

    :param page: Playwright page, registered before the reload.
    :param session_id: Session whose snapshot request is stalled.
    :returns: A single-element list tracking how many snapshot requests were
        held, mutable so the caller can assert the gate was actually exercised.
    """
    held = [0]

    def _handle(route: Route) -> None:
        parsed = urlparse(route.request.url)
        is_snapshot = parsed.path == f"/v1/sessions/{session_id}" and "refresh_state=true" in (
            parsed.query or ""
        )
        if is_snapshot:
            # Hold it open forever: do NOT fulfill / continue / abort. This is
            # the hung-response state the client must survive.
            held[0] += 1
            return
        route.continue_()

    page.route(f"**/v1/sessions/{session_id}*", _handle)
    return held


@pytest.mark.compat_smoke
def test_completed_session_recovers_from_stalled_hydration(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A completed session must not get stuck on "Loading conversation…".

    Opening / reloading a finished session while its hydration snapshot stalls
    must still recover the usable chat surface, instead of hanging on the
    loading placeholder forever.
    """
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "hello there"}],
        key="stalled-hydration",
        match="Say hello",
    )

    # 1. Drive one turn to completion — the session now has committed output.
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill("Say hello")
    page.get_by_role("button", name="Send", exact=True).click()

    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    # 2. Stall the NEXT hydration snapshot, then reload into it. (Registered
    # only now, so the first, successful load above was unaffected.)
    held = _stall_hydration_snapshot(page, session_id)
    page.reload()

    # 3. We must actually hit the loading gate against the stalled snapshot —
    # otherwise the recovery assertion below would pass vacuously. Poll with
    # Playwright's clock (page.wait_for_timeout), which pumps route callbacks;
    # a bare sleep would starve our own route handler.
    loading = page.get_by_text(_LOADING_TEXT, exact=True)
    expect(loading).to_be_visible(timeout=15_000)
    for _ in range(50):
        if held[0] >= 1:
            break
        page.wait_for_timeout(100)
    assert held[0] >= 1, (
        "hydration snapshot request was never intercepted — the reload did not "
        "exercise the stalled-hydration gate this test targets"
    )

    # 4. The guarded bug: the page must not remain stuck on "Loading
    # conversation…". A recovered build clears the placeholder (transcript/
    # composer mount, or an error+retry surfaces) well within the window; a
    # wedged build never does, so this assertion times out.
    expect(loading).to_have_count(0, timeout=_RECOVER_TIMEOUT_MS)
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=_RECOVER_TIMEOUT_MS)
