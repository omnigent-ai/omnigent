"""E2E: switching a session's host must re-attach the web terminal UI.

The switch releases the old runner and launches one on the new host, but the
terminals query caches with ``staleTime: Infinity`` and unions fetched rows
with the cache (``web/src/hooks/useTerminals.ts``), so unless the switch path
explicitly resets ``terminalsQueryKey`` the tab strip keeps showing the
*previous* host's shell.

The runner-online liveness corrector can paper over a missed reset when its
``/health`` poll happens to catch the offline->online edge, which would make
the assertion flaky either way. This test pins the runner online (stable
``/health``) so the corrector stays dormant and the switch path's own cache
reset is what the assertion turns on.

The two hosts and their terminals are stubbed by route interception -- the same
approach as ``test_host_badge.py`` -- because the e2e_ui harness cannot spawn two
real remote hosts. The frontend switch path and terminals cache under test run
for real; the assertion is on the user-visible terminal strip.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry, open_right_rail

_FAKE_HOST_ID = "host_switch_origin"
_TARGET_HOST_ID = "host_switch_target"
# Unix seconds well before now so the patched session is outside the startup
# grace window (STARTING_GRACE_S) and reads its real liveness.
_OLD_CREATED_AT = 1_700_000_000

# Desktop rail tab label is ``f"{terminal_name} · {session_key}"`` (see
# WorkspacePanel's terminalLabelFor). Distinct names per host so the stale tab
# is unmistakable.
_LABEL_HOST_A = "shellA · s1"
_LABEL_HOST_B = "shellB · s1"


@pytest.fixture(autouse=True)
def _drop_routes(page: Page) -> Iterator[None]:
    """Drop this module's route handlers before the page closes.

    The ``/health`` poll and snapshot refetches stay in flight at teardown; a
    handler that replays upstream as the page closes raises TargetClosedError,
    which would surface in the next test's setup.
    """
    yield
    page.unroute_all(behavior="ignoreErrors")


def _terminal_resource(res_id: str, name: str, session: str) -> dict:
    """A ``/resources/terminals`` row in the shape ``terminalInfoFromResource`` reads."""
    return {
        "id": res_id,
        "name": name,
        "metadata": {"terminal_name": name, "session_key": session, "running": True},
    }


def test_switching_host_reattaches_the_web_terminal(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A host switch must move the terminal strip to the new host's shell.

    Journey: open a host-bound session showing host A's shell -> click the host
    badge -> "Switch host..." -> pick host B -> confirm. The terminal strip must
    then show host B's shell, not host A's. Without the switch path's terminals
    cache reset the strip stays on host A's shell and this fails.
    """
    base_url, session_id = seeded_session
    state = {"switched": False}
    release_calls: list[dict] = []
    launch_calls: list[dict] = []

    def _patch_health(route: Route) -> None:
        # Pin the session runner online so the useTerminals liveness corrector
        # stays dormant -- isolating the cache-invalidation defect from the
        # intermittent poll-edge correction.
        response = fetch_with_retry(route)
        payload = response.json()
        live = {"runner_online": True, "host_online": True}
        if isinstance(payload.get("sessions"), dict):
            payload["sessions"][session_id] = live
        if isinstance(payload.get("session"), dict):
            payload["session"] = {**payload["session"], **live}
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_hosts(route: Route) -> None:
        if route.request.method != "GET" or urlparse(route.request.url).path != "/v1/hosts":
            route.continue_()
            return
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "hosts": [
                        {
                            "host_id": _FAKE_HOST_ID,
                            "name": "e2e-origin",
                            "owner": "e2e",
                            "status": "online",
                            "sandbox_provider": None,
                        },
                        {
                            "host_id": _TARGET_HOST_ID,
                            "name": "e2e-target",
                            "owner": "e2e",
                            "status": "online",
                            "sandbox_provider": None,
                        },
                    ]
                }
            ),
        )

    def _patch_filesystem(route: Route) -> None:
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {"entries": [{"path": "/home/e2e/repo", "name": "repo", "is_dir": True}]}
            ),
        )

    def _patch_launch(route: Route) -> None:
        # Host B's runner is now the session's; a subsequent terminals fetch
        # must reflect host B's shell.
        state["switched"] = True
        launch_calls.append(route.request.post_data_json or {})
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"runner_id": "runner_switched"}),
        )

    def _patch_terminals(route: Route) -> None:
        rows = (
            [_terminal_resource("terminal_shellB_s1", "shellB", "s1")]
            if state["switched"]
            else [_terminal_resource("terminal_shellA_s1", "shellA", "s1")]
        )
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"data": rows}),
        )

    def _patch_session(route: Route) -> None:
        request = route.request
        if request.method == "GET":
            response = fetch_with_retry(route)
            payload = response.json()
            # After the switch the session is bound to the new host: the badge
            # follows the snapshot, so it updates while the terminal (the bug)
            # does not.
            payload["host_id"] = _TARGET_HOST_ID if state["switched"] else _FAKE_HOST_ID
            payload["host_resumable"] = True
            payload["created_at"] = _OLD_CREATED_AT
            route.fulfill(
                status=200,
                headers={**response.headers, "content-type": "application/json"},
                body=json.dumps(payload),
            )
            return
        if request.method == "PATCH":
            release_calls.append(request.post_data_json or {})
            route.fulfill(
                status=200,
                headers={"content-type": "application/json"},
                body=json.dumps({"id": session_id, "runner_id": None, "model_override": None}),
            )
            return
        route.fallback()

    # Registered specific-last so Playwright (most-recently-registered wins)
    # routes the session snapshot to _patch_session and its /resources/terminals
    # child to _patch_terminals, while the sidebar list falls through to network.
    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(re.compile(r"/v1/hosts(\?|$)"), _patch_hosts)
    page.route(re.compile(r"/v1/hosts/[^/]+/filesystem"), _patch_filesystem)
    page.route(re.compile(rf"/v1/hosts/{re.escape(_TARGET_HOST_ID)}/runners(\?|$)"), _patch_launch)
    page.route(
        re.compile(rf"/v1/sessions/{re.escape(session_id)}/resources/terminals"),
        _patch_terminals,
    )
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_session)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)

    page.goto(f"{base_url}/c/{session_id}")

    badge = page.get_by_test_id("composer-host-select")
    expect(badge).to_be_visible(timeout=15_000)

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    tab_a = rail.locator(f'[role="button"][title="{_LABEL_HOST_A}"]')
    expect(tab_a).to_be_visible(timeout=15_000)

    badge.click()
    page.get_by_role("menuitem", name="Switch host…", exact=True).click()

    dialog = page.get_by_test_id("switch-host-dialog")
    expect(dialog).to_be_visible(timeout=15_000)
    trigger = page.get_by_test_id("switch-host-select")
    expect(trigger).to_contain_text("e2e-target", timeout=15_000)

    directory = page.get_by_test_id("workspace-path-input")
    expect(directory).to_be_visible(timeout=15_000)
    directory.fill("/home/e2e/repo")
    # Dismiss the path field's suggestion dropdown before submitting: it renders
    # in flow, so closing it shifts the footer and a click on the still-covered
    # submit button would miss.
    dialog.get_by_text("Switch host", exact=True).first.click()
    expect(page.get_by_test_id("workspace-path-dropdown")).to_have_count(0)

    switch = page.get_by_test_id("switch-host-button")
    expect(switch).to_be_enabled(timeout=15_000)
    switch.click()

    expect(dialog).to_have_count(0, timeout=15_000)
    assert [c.get("runner_id") for c in release_calls] == [""], release_calls
    assert [c.get("session_id") for c in launch_calls] == [session_id], launch_calls

    # The switch is done: the terminal strip must show host B's shell and drop
    # host A's. On the buggy build it stays on host A -> tab_b never appears.
    tab_b = rail.locator(f'[role="button"][title="{_LABEL_HOST_B}"]')
    expect(tab_b).to_be_visible(timeout=15_000)
    expect(rail.locator(f'[role="button"][title="{_LABEL_HOST_A}"]')).to_have_count(0)
