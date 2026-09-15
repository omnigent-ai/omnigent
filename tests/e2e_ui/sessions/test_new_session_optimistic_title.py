"""Browser e2e for the optimistic first-prompt label on new sessions.

Drives the real landing flow (create POST, pending-prompt handoff,
client-side navigate) and asserts the sidebar row flips to the typed
prompt with provisional styling, then that a server-side rename
supersedes it over the live update stream with no reload. The /events
POST is stubbed so no turn runs and no seed title races the assertions.

Two setup constraints, both non-obvious:

- The fake create returns session A but the app boots on B. ChatPage's
  initial-prompt consume memoizes per session, so booting on A would
  cache a null consume and the post-create navigate back to A would
  never pick up the held prompt (production create returns a fresh id).
- The recent-workspace seed must land via evaluate AFTER boot (an init
  script is cleared at app boot) so the working-directory chip
  auto-fills and Send enables.
"""

from __future__ import annotations

import json
import re
import time

import httpx
from playwright.sync_api import Page, Route, expect

_PROMPT = "e2e sentinel optimistic label 9f4c2d"
_SETTLED_TITLE = "e2e settled server title"

# Bare create endpoint: ``/v1/sessions`` with an optional query, but NOT
# ``/v1/sessions/{id}/...`` — so reads pass through to the real server
# while only the composer's create POST is faked.
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")


def _fulfill_json(route: Route, body: dict) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def test_new_session_shows_first_prompt_optimistically(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """The landing flow labels the new row with the prompt, provisionally.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session_pair: ``(base_url, session_a, session_b)`` —
        two pre-created runner-bound sessions with no titles. The fake
        create returns A; the app boots on B (see module docstring).
    """
    base_url, session_a, session_b = seeded_session_pair
    event_texts: list[str] = []

    def handle_events(route: Route) -> None:
        # The first-turn send never reaches the server: no turn runs and
        # no seed title is written, so the optimistic label can't be
        # superseded mid-assertion. Recording the text proves the real
        # auto-send path ran.
        body = route.request.post_data_json
        event_texts.append(body["data"]["content"][0]["text"])
        _fulfill_json(route, {"queued": True, "item_id": "ci_e2e"})

    def handle_hosts(route: Route) -> None:
        # One online host so the composer can pick a host (the directly
        # tunneled harness registers no host).
        _fulfill_json(
            route,
            {
                "hosts": [
                    {"host_id": "host_e2e", "name": "e2e-host", "owner": "e2e", "status": "online"}
                ]
            },
        )

    def handle_agents(route: Route) -> None:
        # The composer's available-agent catalog (GET /v1/agents).
        _fulfill_json(
            route,
            {
                "data": [
                    {
                        "id": "ag_e2e",
                        "name": "hello_world",
                        "display_name": "Hello World",
                        "description": None,
                        "harness": None,
                    }
                ]
            },
        )

    def handle_sessions(route: Route) -> None:
        # Fake ONLY the composer's create POST, returning pre-seeded
        # session A so the real handoff targets a real, untitled
        # session. Reads (the list, per-session) pass through.
        if route.request.method == "POST":
            _fulfill_json(route, {"id": session_a})
        else:
            route.continue_()

    page.route("**/v1/sessions/*/events", handle_events)
    page.route("**/v1/hosts", handle_hosts)
    page.route("**/v1/agents", handle_agents)
    page.route(_SESSIONS_RE, handle_sessions)

    # Boot on session B's chat (full reload is fine — the flow below is
    # client-side from here).
    page.goto(f"{base_url}/c/{session_b}")

    # Seed a recent workspace for the stubbed host AFTER boot: the
    # landing's working-directory chip auto-fills from this when the
    # composer mounts, and Send only enables with a valid workspace (the
    # file browser has its own tests).
    page.evaluate(
        """() => localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({ host_e2e: ["/tmp"] }),
        )"""
    )

    # Open the landing composer via the sidebar button: the route change
    # mounts a fresh composer, which reads the seeded recents.
    page.get_by_test_id("new-chat-button").click()

    label_span = page.locator(f'a[href="/c/{session_a}"]').locator("span.relative")

    # Pre-state: session A is untitled, so its row reads as the generic
    # fallback — the flip below must come from the landing flow.
    expect(label_span).to_contain_text("New session")

    prompt_input = page.get_by_test_id("new-chat-landing-input")
    prompt_input.wait_for(state="visible", timeout=15_000)
    prompt_input.fill(_PROMPT)
    # Playwright auto-waits for Send to be actionable (enabled): it
    # enables only once message + host + agent + workspace are all set,
    # so this also confirms the seeded workspace chip auto-filled.
    page.get_by_test_id("new-chat-landing-submit").click()

    page.wait_for_url(re.compile(rf"/c/{re.escape(session_a)}"))

    # The row flips to the typed prompt IMMEDIATELY — before any server
    # round-trip could have titled the session — styled provisionally.
    expect(label_span).to_contain_text(_PROMPT)
    expect(label_span).to_have_class(re.compile(r"\bitalic\b"))

    # Sanity: the real auto-send handoff ran (its POST was intercepted),
    # so a green run isn't a composer that silently never sent.
    # Pump the driver with wait_for_timeout, not time.sleep: the sync API
    # dispatches route handlers only inside Playwright calls, so a bare
    # sleep loop never runs handle_events for a late-landing POST.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _PROMPT not in event_texts:
        page.wait_for_timeout(50)
    assert _PROMPT in event_texts, (
        f"the initial prompt was never POSTed to the session's /events "
        f"(observed: {event_texts}) — the auto-send path did not run"
    )

    # A real server title supersedes the optimistic label: PATCH one via
    # the rename endpoint and the row flips on the live update stream —
    # no reload, so the in-memory optimistic entry is still present and
    # MUST lose, provisional styling and all.
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_a}",
        json={"title": _SETTLED_TITLE},
        timeout=10.0,
    )
    resp.raise_for_status()
    expect(label_span).to_contain_text(_SETTLED_TITLE, timeout=15_000)
    expect(label_span).not_to_have_class(re.compile(r"\bitalic\b"))
