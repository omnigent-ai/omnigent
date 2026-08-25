"""E2E (hermetic): in-session model contract per model-flows-design.md §10.1.

Rows 12–15's hermetic halves. Every test drives the real SPA over the spawned
server, with the browser's view of ONE session shaped into a claude-native
snapshot (the same route-patch idiom as ``test_claude_model_picker.py``) and
SSE frames injected through a captured stream controller — the harness
boundary, not the driven surface.

The assertions encode the DESIGN's target behavior:

- Row 12 (guard): the gear renders its model rows from the already-held
  snapshot; no click-time fetch is load-bearing.
- Row 13: an off-catalog reported model renders as its own appended raw row,
  highlighted; catalog rows highlight only on exact match. Red until step 3.
- Row 14: a pick renders as pending — the chip must NOT flip before the
  harness's own report (the ``session.model`` event) confirms. Red until
  step 6.
- Row 15: a failed switch surfaces the ``model_change_not_applied`` error and
  the chip keeps reporting the pane's real model. Red until step 6.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.chat.test_claude_model_picker import (
    _MODEL_OPTIONS,
    _patch_session_as_claude_native,
)

_STREAM_CONTROLLER = """
(() => {
  const sessionId = __SESSION_ID__;
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    const streamPath = `/v1/sessions/${sessionId}/stream`;
    if (new URL(url, window.location.origin).pathname === streamPath) {
      const body = new ReadableStream({
        start(controller) {
          window.__mfStreamController = controller;
        },
      });
      return Promise.resolve(new Response(body, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }));
    }
    return originalFetch(input, init);
  };
})()
"""


def _install_stream_controller(page: Page, session_id: str) -> None:
    """Capture the session's SSE stream so tests can push frames."""
    page.add_init_script(_STREAM_CONTROLLER.replace("__SESSION_ID__", json.dumps(session_id)))


def _push_sse(page: Page, event: str, payload: dict) -> None:
    """Push one SSE frame through the captured stream controller."""
    page.wait_for_function("window.__mfStreamController !== undefined")
    page.evaluate(
        """
        ({ event, payload }) => {
          const frame = `event: ${event}\\ndata: ${JSON.stringify(payload)}\\n\\n`;
          window.__mfStreamController.enqueue(new TextEncoder().encode(frame));
        }
        """,
        {"event": event, "payload": payload},
    )


def _open_gear_model_dropdown(page: Page) -> None:
    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=15_000)
    gear.click()
    page.get_by_test_id("composer-config-model").click()


def test_row12_gear_rows_render_without_any_click_time_fetch(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The gear's model rows come from the held snapshot, not a click fetch.

    After the page settles, EVERY further ``/v1`` request is aborted; opening
    the gear must still list the catalog rows. (A regression guard — green on
    the current code — pinning the design's "clicking the gear fetches
    nothing load-bearing".)
    """
    base_url, session_id = seeded_session
    _patch_session_as_claude_native(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_test_id("composer-config-gear")).to_be_visible(timeout=15_000)
    # Let the initial snapshot/queries settle before cutting the network.
    page.wait_for_timeout(1_000)

    def _abort_api(route: Route) -> None:
        if "/v1/" in urlparse(route.request.url).path:
            route.abort()
        else:
            route.continue_()

    page.route("**/v1/**", _abort_api)

    _open_gear_model_dropdown(page)
    rows = page.locator('[role="option"][data-model-id]')
    expect(rows).to_have_count(len(_MODEL_OPTIONS))


def test_row13_off_catalog_reported_model_appends_and_highlights_exactly(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """An off-catalog reported model is its own highlighted raw row.

    The session reports ``claude-opus-4-8[1m]`` (a settings-file pin) while
    the catalog holds only alias rows resolving to other models. The design:
    the picker appends the reported value as its own row, highlights it, and
    highlights NO catalog row — never relabeling the report onto a
    same-family row of a different generation. The chip shows the raw
    reported value.
    """
    base_url, session_id = seeded_session
    reported = "claude-opus-4-8[1m]"
    _patch_session_as_claude_native(
        page,
        session_id,
        llm_model=reported,
    )

    page.goto(f"{base_url}/c/{session_id}")

    chip = page.get_by_test_id("composer-model-effort-label")
    expect(chip).to_contain_text(reported, timeout=15_000)

    _open_gear_model_dropdown(page)
    appended = page.locator(f'[role="option"][data-model-id="{reported}"]')
    expect(appended).to_have_count(1)
    expect(appended).to_have_attribute("data-active", "true")
    # The same-family catalog row (Opus 4.10) must NOT claim the highlight.
    expect(page.locator('[role="option"][data-model-id="opus"]')).not_to_have_attribute(
        "data-active", "true"
    )


def test_row13_exact_match_highlights_the_catalog_row(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A reported model exactly matching a row's ``model`` highlights that row."""
    base_url, session_id = seeded_session
    _patch_session_as_claude_native(
        page,
        session_id,
        llm_model="system.ai.claude-sonnet-5",
    )

    page.goto(f"{base_url}/c/{session_id}")
    _open_gear_model_dropdown(page)
    expect(page.locator('[role="option"][data-model-id="sonnet"]')).to_have_attribute(
        "data-active", "true"
    )


def test_row14_pick_stays_pending_until_the_harness_confirms(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The chip flips only on the harness's own report, never on the pick.

    Saving a pick PATCHes the request, but the composer chip must keep the
    reported model (with a pending indicator) until a ``session.model`` event
    carries the harness's confirmation; then it flips to the confirmed row's
    name.
    """
    base_url, session_id = seeded_session
    _install_stream_controller(page, session_id)
    _patch_session_as_claude_native(
        page,
        session_id,
        llm_model="system.ai.claude-sonnet-5",
    )

    page.goto(f"{base_url}/c/{session_id}")
    chip = page.get_by_test_id("composer-model-effort-label")
    expect(chip).to_contain_text("Sonnet 5", timeout=15_000)

    _open_gear_model_dropdown(page)
    page.locator('[role="option"][data-model-id="opus"]').click()
    page.get_by_test_id("composer-config-save").click()

    # Unconfirmed: the chip keeps the reported model and a pending indicator
    # shows. (The PATCH round-trip completes; confirmation has not arrived.)
    page.wait_for_timeout(800)
    expect(chip).to_contain_text("Sonnet 5")
    expect(chip).not_to_contain_text("Opus")
    expect(page.get_by_test_id("composer-model-pending")).to_be_visible()

    # The harness confirms: the report names the model the pane now runs.
    _push_sse(
        page,
        "session.model",
        {"conversation_id": session_id, "model": "system.ai.claude-opus-4-10"},
    )
    expect(chip).to_contain_text("Opus 4.10", timeout=10_000)
    expect(page.get_by_test_id("composer-model-pending")).to_have_count(0)


def test_row15_failed_switch_surfaces_error_and_keeps_the_reported_model(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A swallowed switch shows the not-applied error; the chip never lies.

    The runner reports failure (the server publishes the
    ``model_change_not_applied`` error event) and no confirmation ever
    arrives: the visible error must name the failure, and the chip must keep
    the pane's real model instead of claiming the pick.
    """
    base_url, session_id = seeded_session
    _install_stream_controller(page, session_id)
    _patch_session_as_claude_native(
        page,
        session_id,
        llm_model="system.ai.claude-sonnet-5",
    )

    page.goto(f"{base_url}/c/{session_id}")
    chip = page.get_by_test_id("composer-model-effort-label")
    expect(chip).to_contain_text("Sonnet 5", timeout=15_000)

    _open_gear_model_dropdown(page)
    page.locator('[role="option"][data-model-id="haiku"]').click()
    page.get_by_test_id("composer-config-save").click()

    _push_sse(
        page,
        "response.error",
        {
            "source": "execution",
            "error": {
                "code": "model_change_not_applied",
                "message": (
                    "The terminal was not switched to haiku: the runner returned "
                    "status 503. It is still running on its previous model."
                ),
            },
        },
    )

    # The failure surfaces as the app's standard error pill: a headline is
    # always visible, and its collapsed detail carries the specific reason.
    headline = page.get_by_test_id("error-headline").first
    expect(headline).to_be_visible(timeout=10_000)
    # Expand to read the detail. The click can land before the disclosure
    # handler is wired under suite load, so retry until the detail shows.
    detail = page.get_by_text("was not switched", exact=False).first
    for _ in range(5):
        headline.click()
        try:
            expect(detail).to_be_visible(timeout=2_000)
            break
        except AssertionError:
            continue
    expect(detail).to_be_visible(timeout=5_000)
    # The chip never claimed the pick: it keeps the pane's reported model.
    expect(chip).to_contain_text("Sonnet 5")
    expect(chip).not_to_contain_text("Haiku")
