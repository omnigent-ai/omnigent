"""E2E: the slash-command skill menu survives a runner-online refresh.

Covers the user-facing half of the "skills vanish mid-session" fix.
Runner liveness is poll-driven, and an unknown→true edge fires
``useRefreshSessionStateOnRunnerOnline`` → ``refreshSessionState``, whose
``refresh_state=true`` snapshot can answer ``skills: []`` for a session
that has skills (the server resolves skills off the snapshot hot path,
and the refresh cold-drops its cache). Before the fix the client applied
that empty list over the composer's slash-command menu — directly on the
refresh path, and also via the ``session_skills`` nudge refetch deduping
onto the in-flight refresh query — and the menu stayed blank until the
conversation was re-opened. The fixed client treats an empty-skills
refetch response as "not yet known" and keeps the menu.

The test drives that sequence in a real browser against the live server,
controlling the inputs that make it deterministic:

- ``GET /health?session_ids=`` is aborted until the test arms, so runner
  liveness stays *unknown* (no offline banner, no remount); the first
  fulfilled poll then answers online — the exact edge the refresh hook
  fires on. The sidebar's other liveness sources are neutralized so the
  poll owns the edge: the conversations list is proxied with its
  ``runner_online`` fields stripped, and the ``/v1/sessions/updates``
  WS is mocked open-but-silent.
- ``GET /v1/sessions/{id}`` is proxied to the real server with a skill
  injected (the seeded ``hello_world`` bundle carries none). Once armed,
  refresh-state requests answer ``skills: []`` — the pre-fix server's
  race — and plain snapshot reads are failed so a recovery refetch
  can't quietly re-inject the skill and mask a client regression.

Selectors mirror ``SlashCommandMenu``: skill rows render as
``data-testid="slash-menu-item-<name>"``.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, Route, expect

_SKILL = {"name": "brainstorming", "description": "Explore an idea"}

# The liveness poll retries with backoff while /health errors, so the first
# fulfilled response lands within one backoff step of arming; two poll
# cycles (10s each) plus slack bounds the wait.
_EDGE_WAIT_MS = 25_000


def _open_menu_row(page: Page) -> None:
    """Type the skill's slash query and assert its menu row is visible.

    Clears the composer first so repeated calls start from the same state.

    :param page: Playwright page on an open ``/c/{id}`` chat.
    """
    composer = page.get_by_label("Message the agent")
    composer.fill("")
    composer.fill(f"/{_SKILL['name']}")
    row = page.get_by_test_id(f"slash-menu-item-{_SKILL['name']}")
    expect(row).to_be_visible(timeout=10_000)


@pytest.mark.timeout(180)
def test_skill_menu_survives_runner_online_refresh(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The ``/`` menu keeps its skills across a runner-liveness refresh.

    Sequence: menu populated while liveness is unknown → the first health
    poll answers online (the edge fires ``refreshSessionState``, whose
    stubbed response reports no skills) → the menu must still list the
    skill. Pre-fix, the empty refresh response wiped the menu here.

    :param page: Playwright page (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` from the fixture.
    """
    base_url, session_id = seeded_session

    state = {"armed": False, "empty_refreshes": 0}

    def _health(route: Route) -> None:
        if not state["armed"]:
            # Unknown liveness: the poller treats the error as "not yet
            # polled" and backs off, so no offline UI renders meanwhile.
            route.abort()
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "sessions": {
                        session_id: {
                            "runner_online": True,
                            "host_online": None,
                            "host_version": None,
                        }
                    }
                }
            ),
        )

    def _strip_list_liveness(route: Route) -> None:
        upstream = route.fetch()
        body = upstream.json()
        for row in body.get("data") or []:
            row.pop("runner_online", None)
            row.pop("host_online", None)
        route.fulfill(
            status=upstream.status,
            content_type="application/json",
            body=json.dumps(body),
        )

    def _session_snapshot(route: Route) -> None:
        if route.request.method != "GET":
            route.fallback()
            return
        is_refresh = "refresh_state=true" in route.request.url
        if state["armed"] and not is_refresh:
            # The server's `session_skills` recovery event triggers a plain
            # refetch that would re-inject the skill and could mask a
            # clobber; fail it (the store keeps current state on a failed
            # refetch) so the refresh response alone decides the outcome.
            route.abort()
            return
        upstream = route.fetch()
        body = upstream.json()
        if state["armed"] and is_refresh:
            body["skills"] = []
            state["empty_refreshes"] += 1
        else:
            body["skills"] = [_SKILL]
        route.fulfill(
            status=upstream.status,
            content_type="application/json",
            body=json.dumps(body),
        )

    page.route("**/health?session_ids=*", _health)
    page.route(re.compile(r"/v1/sessions\?"), _strip_list_liveness)
    page.route(re.compile(rf"/v1/sessions/{session_id}(\?|$)"), _session_snapshot)
    # Mock the sessions-updates WS open-but-silent so the sidebar stream
    # can't leak runner liveness into the merged map ahead of the poll.
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)

    # Phase 1 — liveness unknown, menu populated from the injected snapshot.
    _open_menu_row(page)

    # Phase 2 — arm the failure mode and let the next poll answer online.
    # The unknown→true edge fires refreshSessionState; its stubbed response
    # reports no skills. Wait for that response to have been served.
    state["armed"] = True
    waited = 0
    while state["empty_refreshes"] == 0:
        assert waited < _EDGE_WAIT_MS, "runner-online refresh never fired"
        page.wait_for_timeout(250)
        waited += 250
    page.wait_for_timeout(1_000)

    # Phase 3 — the menu must still resolve the skill. Pre-fix, the empty
    # refresh response blanked `skills` here and this row disappeared.
    _open_menu_row(page)
