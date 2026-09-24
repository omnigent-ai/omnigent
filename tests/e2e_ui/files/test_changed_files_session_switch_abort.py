"""E2E: switching sessions cancels the pending changed-files request.

The changed-files view fetches
``/v1/sessions/<id>/resources/environments/<env>/changes`` through TanStack
Query (``useWorkspaceChangedFiles``). TanStack hands the query function an
``AbortSignal`` and aborts it when the query's last observer unmounts — which
is exactly what happens when the user switches to another session while the
request is still in flight. The hook must forward that signal to
``authenticatedFetch`` so the obsolete request is actually cancelled at the
network layer; when it is dropped, the stale request keeps consuming network
and runner/server work after the switch, and rapid switching stacks several
such requests.

There is no on-screen error to assert on — the user-visible journey is just
"open a session, switch to another one" and the leak is only observable at
the network layer. The reproduction therefore holds session A's ``/changes``
request open (an unhandled Playwright route keeps it in flight), switches to
session B through the real sidebar link, and then requires the held request
to be aborted. Cancellation is detected both from the network side (the
``requestfailed`` event) and from inside the page (a ``window.fetch`` probe
that records the request promise rejecting with ``AbortError``), so the test
does not depend on either detector's quirks.

A document-load marker pins the switch to an in-place SPA navigation: a full
reload would abort every in-flight fetch and make the test pass without the
query cancellation working.
"""

from __future__ import annotations

import re
import time

from playwright.sync_api import Page, Request, Route, expect

from tests.e2e_ui.conftest import open_right_rail

# How long the prior session's request may stay in flight after the switch
# before it counts as leaked. Cancellation lands immediately when the signal
# is wired through; the slack only absorbs CI scheduling lag.
_CANCEL_WAIT_S = 10.0

_INIT_SCRIPT = """
window.__documentLoadMarker = crypto.randomUUID();
window.__changesFetches = [];
(() => {
  const orig = window.fetch;
  window.fetch = function (...args) {
    let url = "";
    try {
      url = typeof args[0] === "string" ? args[0] : args[0].url;
    } catch {
      // Leave non-URL inputs unrecorded.
    }
    if (!/\\/resources\\/environments\\/[^/]+\\/changes(\\?|$)/.test(url)) {
      return orig.apply(this, args);
    }
    const entry = { url, outcome: "pending" };
    window.__changesFetches.push(entry);
    const result = orig.apply(this, args);
    result.then(
      () => {
        entry.outcome = "resolved";
      },
      (err) => {
        entry.outcome = (err && err.name) || "rejected";
      },
    );
    return result;
  };
})();
"""


def test_session_switch_cancels_pending_changed_files_request(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Leaving a session must abort its in-flight ``/changes`` request.

    :param page: Fresh Playwright page.
    :param seeded_session_pair: ``(base_url, session_a, session_b)`` for two
        real sessions bound to the same live runner.
    """
    base_url, session_a, session_b = seeded_session_pair

    changes_a = re.compile(
        rf"/v1/sessions/{re.escape(session_a)}/resources/environments/[^/]+/changes(\?|$)"
    )

    held: list[Route] = []

    def _hold_changes(route: Route) -> None:
        # Deliberately unhandled: the request stays in flight until the
        # browser itself aborts it, which is what the switch must trigger.
        held.append(route)

    page.route(changes_a, _hold_changes)

    cancelled: list[str] = []

    def _on_request_failed(request: Request) -> None:
        if changes_a.search(request.url):
            cancelled.append(request.failure or "failed")

    page.on("requestfailed", _on_request_failed)
    page.add_init_script(_INIT_SCRIPT)

    page.goto(f"{base_url}/c/{session_a}")
    load_marker = page.evaluate("window.__documentLoadMarker")

    # Show the panel the request feeds so the journey matches what a user
    # sees (the Changes tab sits on its loading state while the request is
    # held). The fetch itself already fired from the session shell.
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    changes_tab = rail.get_by_role("tab", name=re.compile("^Changes"))
    changes_tab.click()
    expect(changes_tab).to_have_attribute("aria-selected", "true")

    # The changed-files GET fires once the environment probe reports the
    # workspace available; it then parks on the stalled route, still pending.
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline and not held:
        page.wait_for_timeout(200)
    assert held, "session A's changed-files request never fired"

    # Switch to session B in place: session A's query loses its observers
    # while its request is still in flight.
    page.locator(f'a[href="/c/{session_b}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_b}", timeout=30_000)
    assert page.evaluate("window.__documentLoadMarker") == load_marker, (
        "session switch reloaded the document; the reproduction needs an in-place SPA navigation"
    )

    def _session_a_outcomes() -> list[str]:
        entries = page.evaluate("window.__changesFetches")
        return [e["outcome"] for e in entries if session_a in e["url"]]

    deadline = time.monotonic() + _CANCEL_WAIT_S
    while time.monotonic() < deadline:
        if cancelled or "AbortError" in _session_a_outcomes():
            break
        page.wait_for_timeout(200)

    outcomes = _session_a_outcomes()
    assert cancelled or "AbortError" in outcomes, (
        "session A's pending changed-files request was not cancelled within "
        f"{_CANCEL_WAIT_S:.0f}s of switching to session B "
        f"(fetch outcomes for session A: {outcomes}); the obsolete request "
        "is still in flight"
    )
