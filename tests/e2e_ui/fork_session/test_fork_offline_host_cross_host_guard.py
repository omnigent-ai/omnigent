"""Browser e2e: forking a host-bound session must not silently land on a different host.

Reproduces the incident: a session ran on host A.  When the user forks it
while host A is offline (or otherwise not the picker's default), the
fork dialog silently defaults the clone to a *different* online host
and lets the fork proceed.  Cross-host forking is not supported today
(confirmed by the team), so the fork is created, its runner launch never
comes up, and the user's first message in the clone fails with the
generic::

    The runner for this session is not available — it may have failed to
    start. See the host logs.

The ticket's ask: the UI should make the cross-host limitation clear
rather than surfacing a generic failure after the fact.

What the dialog does today (the bug):

- ``ForkSessionDialog`` defaults ``selectedHostId`` to the source host only
  when it is ONLINE; otherwise it silently picks the FIRST online host — a
  different machine — with no indication that a cross-host fork will fail.
- The only cross-host hint is the soft directory-mismatch note ("Earlier
  file references in the transcript may not apply — the agent will need to
  re-orient"), which appears only after a directory is typed and says
  nothing about the fork being unsupported or failing.
- Submit stays enabled; the fork is created and the runner launch fires at
  the different host.

Expected fix (either direction passes this test):

- a clear cross-host notice/block in the dialog before submit (preferred
  testid: ``fork-session-cross-host-warning``), or
- no silent cross-host default (e.g. keep the host unselected with
  guidance to reconnect the source host).

Test shape
----------
No real ``omnigent host`` is needed — the host-side wire is stubbed at the
network layer (same pattern as ``test_fork_deleted_worktree_recreate``):

- ``GET /v1/hosts`` → the source host OFFLINE + a different host ONLINE.
- ``GET /v1/sessions/{id}`` → patched with the source host + workspace.
- ``GET /v1/hosts/{other}/filesystem/**`` → 200 (directory exists there).
- ``POST /v1/hosts/{other}/runners`` → records the launch body (the launch
  that, in production, never comes up — leaving the clone unbound).
- ``POST /v1/sessions/{id}/fork`` → passes through to the real server.

The test drives the full user journey first (so a recording captures it
end to end) and asserts LAST:

1. The dialog auto-picks the different host (the silent cross-host
   default).  If a fix removed that default, the test passes early.
2. Before submit, the dialog must clearly flag the cross-host limitation
   (or disable submit).  If it does, the fix landed and the test passes.
3. Otherwise the journey is driven to its broken end state — the fork is
   created, the runner launch fires at the WRONG host — and the test fails
   with a bug-keyed message.  This is today's behavior.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm, fetch_with_retry

# Unique marker so other tests' transcripts can't satisfy this test's
# content assertions.
_XHOST_MARKER = "cobalt-cross-host-fork-marker"

# Fake host geometry mirroring the incident: the source session is bound
# to a host that is OFFLINE at fork time, while a different host is online.
_SRC_HOST_ID = "host_arca_e2e_src"
_SRC_HOST_NAME = "arca-e2e-src"
_OTHER_HOST_ID = "host_dbx_sandbox_e2e"
_OTHER_HOST_NAME = "dbx-sandbox-e2e"
_SRC_WS = "/work/project"
_FORK_WS = "/work/elsewhere"


def _cross_host_clearly_flagged(dialog: Locator) -> bool:
    """Whether the dialog clearly communicates the cross-host limitation.

    Accepts either the preferred dedicated element
    (``fork-session-cross-host-warning``) or any dialog text that plainly
    says a cross-host fork is unsupported / will fail.  The existing soft
    directory-mismatch note ("Earlier file references … may not apply")
    intentionally does NOT match: it says nothing about the fork failing.

    :param dialog: The fork dialog root locator.
    :returns: ``True`` when a clear cross-host notice is visible.
    """
    notice = dialog.get_by_test_id("fork-session-cross-host-warning")
    if notice.count() > 0 and notice.first.is_visible():
        return True
    text = dialog.inner_text()
    lowered = text.lower()
    if "host" not in lowered:
        return False
    return bool(
        re.search(
            r"(not\s+(yet\s+)?supported|isn['’]t\s+supported|unsupported"
            r"|cannot\s+(be\s+)?fork|can['’]t\s+(be\s+)?fork"
            r"|will\s+fail|won['’]t\s+(start|work))",
            lowered,
        )
    )


def test_fork_of_offline_host_session_must_not_silently_land_on_another_host(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Cross-host fork must be clearly flagged, not silently created broken.

    The source session is bound to an offline host.  Opening the fork
    dialog must not end in a silently-created cross-host fork whose runner
    never starts.  Unfixed, it does: the dialog defaults to a different
    online host, shows no cross-host-unsupported notice, and the submit
    creates the fork + fires the runner launch at the wrong host.  The
    test fails at that point with a bug-keyed message.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    :param mock_llm_server_url: Session-scoped mock LLM server URL; used to
        script the seed turn so no real credentials are needed.
    """
    base_url, session_id = seeded_session

    runner_bodies: list[dict[str, Any]] = []
    fork_calls: list[str] = []

    # ── Network stubs ──────────────────────────────────────────────────

    def handle_hosts(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "hosts": [
                        {
                            "host_id": _SRC_HOST_ID,
                            "name": _SRC_HOST_NAME,
                            "owner": "e2e",
                            "status": "offline",
                            "configured_harnesses": {},
                        },
                        {
                            "host_id": _OTHER_HOST_ID,
                            "name": _OTHER_HOST_NAME,
                            "owner": "e2e",
                            "status": "online",
                            "configured_harnesses": {},
                        },
                    ]
                }
            ),
        )

    def handle_session_detail(route: Route) -> None:
        # Patch the real session so it reads as a coding session bound to
        # the (offline) source host.  Non-GET traffic (e.g. PATCH) passes
        # through untouched so the fork call itself is real.
        if route.request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        body = response.json()
        body["host_id"] = _SRC_HOST_ID
        body["workspace"] = _SRC_WS
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def handle_filesystem(route: Route) -> None:
        # Every path on the OTHER host is listable, so the pre-flight for
        # the picked directory passes and the fork proceeds (as it did in
        # the incident — the directory existed; the RUNNER never started).
        decoded_url = urllib.parse.unquote(route.request.url)
        assert _OTHER_HOST_ID in decoded_url
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"object": "list", "data": [], "has_more": False}),
        )

    def handle_runners(route: Route) -> None:
        runner_bodies.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"runner_id": "runner_e2e_xhost", "status": "launching"}),
        )

    def handle_fork(route: Route) -> None:
        fork_calls.append(route.request.url)
        route.continue_()

    page.route("**/v1/hosts", handle_hosts)
    page.route(re.compile(r".*/v1/hosts(\?.*)?$"), handle_hosts)
    # Regex so the slim snapshot variant (``?include_items=false&…``) is
    # also patched — otherwise the props feeding the dialog see the
    # unpatched session and take the non-coding path.
    page.route(
        re.compile(rf".*/v1/sessions/{re.escape(session_id)}(\?.*)?$"),
        handle_session_detail,
    )
    page.route(f"**/v1/hosts/{_OTHER_HOST_ID}/filesystem/**", handle_filesystem)
    page.route(f"**/v1/hosts/{_OTHER_HOST_ID}/runners", handle_runners)
    page.route("**/v1/sessions/*/fork", handle_fork)

    # ── Seed one turn so the dialog has an assistant bubble to anchor on ──

    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "OK"}],
        key="fork-xhost-seed",
        match=_XHOST_MARKER,
    )

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()
    composer.fill(f"Reply with one short word. Marker: {_XHOST_MARKER}")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_be_visible(timeout=60_000)

    # ── Open the fork dialog ───────────────────────────────────────────

    assistant.hover()
    page.get_by_test_id("fork-from-response").first.click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()

    # A coding source (workspace present + online host) shows "Clone & start".
    submit = page.get_by_test_id("fork-session-submit")
    expect(submit).to_have_text("Clone & start")

    # ── Observe the silent cross-host default ─────────────────────────
    # The source host is offline, so today the dialog auto-picks the first
    # ONLINE host — a different machine.  If a fix removed this silent
    # default (e.g. leaves the host unpicked with guidance), the bug's
    # trigger is gone and the test passes.
    host_trigger = page.get_by_test_id("fork-session-host-select")
    expect(host_trigger).to_be_visible()
    try:
        expect(host_trigger).to_contain_text(_OTHER_HOST_NAME, timeout=10_000)
    except AssertionError:
        return  # no silent cross-host default — fixed behavior

    # ── Pick a directory on the different host ─────────────────────────
    # Advanced auto-expands on a different host; expand defensively if not.
    ws_input = page.get_by_test_id("workspace-path-input")
    if not ws_input.is_visible():
        page.get_by_test_id("fork-session-advanced-toggle").click()
    expect(ws_input).to_be_visible()
    ws_input.fill(_FORK_WS)

    # Give any cross-host notice a beat to render before judging clarity.
    page.wait_for_timeout(1_000)

    # ── The fix contract: flag it clearly, or don't let it proceed ─────
    if _cross_host_clearly_flagged(dialog):
        return  # the dialog now communicates the cross-host limitation
    if not submit.is_enabled():
        return  # the dialog now blocks the unsupported cross-host fork

    # Today the ONLY hint is the soft file-references note — which says
    # nothing about the fork being unsupported or failing.  Its presence
    # here documents that the dialog *knows* the host differs and still
    # lets the fork proceed silently.
    expect(page.get_by_test_id("fork-session-mismatch-warning")).to_be_visible()

    # ── Drive the silent cross-host fork to its broken end state ──────
    submit.click()

    # The fork is created and the page navigates into it.
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})(conv_)?[0-9a-f]+"),
        timeout=30_000,
    )
    fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]
    assert fork_id != session_id
    assert fork_calls, "expected the fork POST to have been sent"

    # The runner launch fired at the DIFFERENT host — the launch that in
    # production never comes up, leaving the clone broken ("The runner for
    # this session is not available — it may have failed to start.").
    deadline = time.monotonic() + 30.0
    while not runner_bodies and time.monotonic() < deadline:
        time.sleep(0.2)
    assert len(runner_bodies) == 1, "expected exactly one background runner launch"
    launch = runner_bodies[0]
    assert launch["session_id"] == fork_id, launch
    assert launch["workspace"] == _FORK_WS, launch

    # Let the fork's landing state render for the journey recording.
    page.wait_for_timeout(2_500)

    pytest.fail(
        "Cross-host fork bug reproduced: the fork of a session bound to offline host "
        f"'{_SRC_HOST_NAME}' silently defaulted to different host "
        f"'{_OTHER_HOST_NAME}', showed no cross-host-unsupported notice, and "
        f"created fork {fork_id} whose runner launch targeted the wrong host "
        "— the fork lands broken with a generic runner-not-available failure "
        "instead of a clear cross-host message."
    )
