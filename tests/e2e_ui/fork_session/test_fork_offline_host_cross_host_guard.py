"""Browser e2e: forking a host-bound session must not silently land on a different host.

An offline source host is not silently replaced: the clone's target stays
unpicked, a hint names the host and asks for a reconnect, and "Clone & start"
stays greyed. An explicit cross-host choice remains allowed and is visibly
warned before any directory is typed. The two-host state and the session's
host/workspace are network stubs: the harness can't register two real hosts,
and the logic under test is client-side.
"""

from __future__ import annotations

import json
import os
import re

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm, fetch_with_retry

# Unique marker so other tests' transcripts can't satisfy the content checks.
_XHOST_MARKER = "cobalt-cross-host-fork-marker"

_SRC_HOST_ID = "host_arca_e2e_src"
_SRC_HOST_NAME = "arca-e2e-src"
_OTHER_HOST_ID = "host_dbx_sandbox_e2e"
_OTHER_HOST_NAME = "dbx-sandbox-e2e"
_SRC_WS = "/work/project"
_FORK_WS = "/work/elsewhere"


def _hold_for_recording(page: Page) -> None:
    """Keep a verified state on screen long enough to read when filming."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(1_500)


@pytest.mark.workspace_panel_product_default
def test_fork_of_offline_host_session_must_not_silently_land_on_another_host(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """The dialog explains the offline source host instead of picking another."""
    base_url, session_id = seeded_session

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
        # Make the real session read as a coding session bound to the offline
        # source host; non-GET traffic passes through untouched.
        if route.request.method != "GET":
            route.continue_()
            return
        body = fetch_with_retry(route).json()
        body["host_id"] = _SRC_HOST_ID
        body["workspace"] = _SRC_WS
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def handle_filesystem(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"object": "list", "data": [], "has_more": False}),
        )

    # Non-browser setup first; the recorded page is created afterward so a
    # video starts at the journey, not at fixture setup.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "OK"}],
        key="fork-xhost-seed",
        match=_XHOST_MARKER,
    )

    page: Page = request.getfixturevalue("page")

    page.route(re.compile(r".*/v1/hosts(\?.*)?$"), handle_hosts)
    # The regex also catches the slim snapshot variant (``?include_items=false``);
    # otherwise the dialog props see the unpatched session and take the
    # non-coding path.
    page.route(
        re.compile(rf".*/v1/sessions/{re.escape(session_id)}(\?.*)?$"),
        handle_session_detail,
    )
    page.route(
        re.compile(rf".*/v1/hosts/{re.escape(_OTHER_HOST_ID)}/filesystem([/?].*)?$"),
        handle_filesystem,
    )

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()
    composer.fill(f"Reply with one short word. Marker: {_XHOST_MARKER}")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_be_visible(timeout=60_000)

    assistant.hover()
    page.get_by_test_id("fork-from-response").first.click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()
    submit = page.get_by_test_id("fork-session-submit")
    expect(submit).to_have_text("Clone & start")

    # Offline source host: no silent auto-pick of another host.
    host_trigger = page.get_by_test_id("fork-session-host-select")
    expect(host_trigger).to_be_visible()
    expect(host_trigger).not_to_contain_text(_OTHER_HOST_NAME)
    hint = page.get_by_test_id("fork-session-source-host-offline-hint")
    expect(hint).to_be_visible()
    expect(hint).to_contain_text(_SRC_HOST_NAME)
    expect(hint).to_contain_text("isn't supported")
    expect(submit).to_be_disabled()
    _hold_for_recording(page)

    # An explicit cross-host pick is still allowed, but it is flagged plainly
    # before any directory is typed -- not just the soft file-references note.
    host_trigger.click()
    page.get_by_test_id(f"fork-session-host-option-{_OTHER_HOST_ID}").click()
    expect(host_trigger).to_contain_text(_OTHER_HOST_NAME)
    warning = page.get_by_test_id("fork-session-cross-host-warning")
    expect(warning).to_be_visible()
    expect(warning).to_contain_text("may fail to start")
    expect(hint).to_have_count(0)

    # A different host auto-expands Advanced so a directory there can be picked.
    ws_input = page.get_by_test_id("workspace-path-input")
    expect(ws_input).to_be_visible()
    ws_input.fill(_FORK_WS)
    expect(warning).to_be_visible()
    expect(page.get_by_test_id("fork-session-mismatch-warning")).to_have_count(0)
    expect(submit).to_be_enabled()
    _hold_for_recording(page)
