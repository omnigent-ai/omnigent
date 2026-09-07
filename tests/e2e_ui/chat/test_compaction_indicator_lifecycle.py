"""E2E regression: compaction indicator lifecycle in the web UI.

The web UI renders a "Compacting conversation…" spinner with an animated bar
and an elapsed-seconds counter while a session compacts its context:

* ``response.compaction.in_progress``  → a ``compaction_loading`` bubble
  (``CompactionLoadingIndicator``, ``data-testid="compacting-indicator"``).
* ``response.compaction.completed``    → replaces it with the permanent
  "Conversation compacted" marker (``CompactionMarker``) and stops the timer.

Both tests drive the REAL server path a native session's compaction takes: the
``external_compaction_status`` event on ``POST /v1/sessions/{id}/events`` (what
the native forwarders post), which the server republishes as the standard
``response.compaction.{in_progress,completed}`` SSE events the web UI renders.
A long compaction posts ``in_progress`` repeatedly — once per status poll — so
the client must treat repeated progress reports as ONE compaction:

* one live spinner at a time — a repeated ``in_progress`` refreshes the
  existing spinner instead of stacking another;
* completion clears every spinner, leaving only the permanent marker (no
  orphaned spinner left counting and flashing beside it);
* the elapsed counter is anchored to the true compaction start — the
  server-reported ``started_at`` — so it survives a page reload instead of
  restarting from the component remount.
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

_COMPOSER = "Send a message…"
_INDICATOR = '[data-testid="compacting-indicator"]'
_MARKER_TEXT = "Conversation compacted"
# Matches the "(647s)" elapsed-seconds suffix in the indicator text.
_ELAPSED_RE = re.compile(r"\((\d+)s\)")


def _post_compaction_status(base_url: str, session_id: str, status: str) -> None:
    """Post the compaction edge a native forwarder emits.

    The server republishes this as the standard
    ``response.compaction.{in_progress,completed,failed}`` SSE event, so this is
    the genuine end-to-end path a native session's compaction takes to the web
    UI — not an internal shortcut.

    :param base_url: Spawned server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param status: One of ``"in_progress"``, ``"completed"``, ``"failed"``.
    """
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_compaction_status", "data": {"status": status}},
        timeout=10.0,
    )
    resp.raise_for_status()


def _elapsed_seconds(page: Page) -> int | None:
    """Read the last compaction indicator's elapsed-seconds counter, if shown.

    :param page: Playwright page.
    :returns: The integer inside ``(Ns)`` in the (last) indicator, ``0`` when an
        indicator is visible but shows no counter yet, or ``None`` when no
        indicator is present.
    """
    indicator = page.locator(_INDICATOR)
    if indicator.count() == 0:
        return None
    match = _ELAPSED_RE.search(indicator.last.inner_text())
    return int(match.group(1)) if match else 0


def test_completion_leaves_no_stale_compaction_spinner(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """One compaction shows one spinner, and completion clears it.

    A long compaction reports progress more than once before it finishes (the
    native forwarders post ``in_progress`` on every status poll). Repeated
    progress reports must refresh the one live spinner — not stack additional
    spinner bubbles — and the single ``completed`` event must leave only the
    "Conversation compacted" marker, with no spinner left counting and
    flashing beside it.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a live server session.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=20_000)

    indicator = page.locator(_INDICATOR)
    marker = page.get_by_text(_MARKER_TEXT)

    # Compaction starts and, being slow, re-announces progress a second later.
    _post_compaction_status(base_url, session_id, "in_progress")
    expect(indicator).to_have_count(1, timeout=10_000)
    page.wait_for_timeout(1200)
    _post_compaction_status(base_url, session_id, "in_progress")

    # Give the repeated announcement time to arrive, then require it refreshed
    # the existing spinner rather than stacking a second one.
    page.wait_for_timeout(1200)
    expect(indicator).to_have_count(1)

    # Let the spinner clearly start counting, so what completion clears is
    # unmistakably a live, climbing timer rather than a momentary flash.
    page.wait_for_timeout(2500)
    assert (_elapsed_seconds(page) or 0) >= 1, (
        "compaction spinner never started counting — the in_progress events did "
        "not render a live indicator"
    )

    # Compaction finishes: a single response.compaction.completed event.
    _post_compaction_status(base_url, session_id, "completed")

    # The permanent "Conversation compacted" marker appears...
    expect(marker).to_be_visible(timeout=10_000)

    # ...and EVERY "Compacting…" spinner is gone: the user sees a single
    # spinner→marker transition, never an orphaned spinner still counting
    # beside the marker.
    expect(indicator).to_have_count(0, timeout=5_000)


def test_compaction_elapsed_time_survives_reload(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Reloading during an active compaction must preserve elapsed time.

    While compaction is active the spinner counts up from the true start.
    Reloading the page must not restart the counter from ~0: when the
    still-running compaction is re-announced on the fresh stream, the
    indicator must continue from the real compaction start rather than from
    the component's remount time.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a live server session.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=20_000)

    # Compaction starts; let the counter climb to a clearly non-zero value.
    _post_compaction_status(base_url, session_id, "in_progress")
    indicator = page.locator(_INDICATOR)
    expect(indicator).to_be_visible(timeout=10_000)
    page.wait_for_timeout(5000)
    elapsed_before = _elapsed_seconds(page) or 0
    assert elapsed_before >= 3, (
        f"expected the compaction counter to reach >= 3s before reload, got {elapsed_before}s"
    )

    # Reload the page. Compaction is still genuinely active, so re-announce it
    # the way a real still-running compaction would appear on the fresh stream
    # (the live stream has no replay). The elapsed counter must continue from
    # the real start — not restart from this remount.
    page.reload()
    expect(composer).to_be_visible(timeout=20_000)
    _post_compaction_status(base_url, session_id, "in_progress")
    expect(indicator).to_be_visible(timeout=10_000)
    page.wait_for_timeout(1500)
    elapsed_after = _elapsed_seconds(page) or 0

    # The elapsed time is preserved across the reload: the counter continues
    # from the true compaction start instead of resetting to ~0.
    assert elapsed_after >= elapsed_before, (
        f"compaction elapsed time reset on reload: showed {elapsed_before}s "
        f"before reload but only {elapsed_after}s after — the counter is anchored "
        f"to the component remount, not the compaction start"
    )
