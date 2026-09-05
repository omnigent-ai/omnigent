"""Browser e2e: forking a sandbox-hosted session must offer a launch target.

The SPA's only fork entry point is the per-message "Fork from here" action
on assistant bubbles (there is deliberately no header/menu Clone button).
For a session whose compute is a server-managed sandbox host, that action
opens the fork dialog — but if the dialog's host picker drops every host
carrying a ``sandbox_provider``, a managed-deployment user whose only
compute is the sandbox dead-ends: the dialog renders the "No hosts
connected yet" connect instructions and the submit button never enables,
so the fork affordance is unusable for exactly the sessions the managed
deployment creates.

The e2e harness cannot provision a real managed sandbox (no provider
credentials), so the browser's view of a real seeded session is patched into
the sandbox-hosted shape via route interception — the same approach as
``tests/e2e_ui/sessions/test_host_badge.py``:

- ``GET /v1/sessions/{id}`` → ``host_id`` bound to a sandbox host and a
  ``workspace`` set, so the source reads as a coding session (the fork
  dialog shows its host/directory fields).
- ``GET /v1/hosts`` → exactly one host: the ONLINE sandbox host backing the
  session (``sandbox_provider: "lakebox"``) — the shape of a managed-
  deployment user with no personal machines connected.
- ``GET /health`` → the session reports ``host_online``/``runner_online`` so
  the chat surface renders normally (no reconnect dead-end).
- ``WS /v1/sessions/updates`` → blocked so a live push can't revert the
  patched shape mid-test.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry, seed_committed_turn

_SANDBOX_HOST_ID = "host_sandbox_managed"
_SANDBOX_WORKSPACE = "/workspace/demo-repo"
# Unix seconds well before now so liveness reads the patched /health values,
# not the ``starting`` grace window (see useSessionLiveness).
_OLD_CREATED_AT = 1_700_000_000

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'


@pytest.fixture(autouse=True)
def _drop_routes(page: Page) -> Iterator[None]:
    """Unroute this module's handlers before the page closes.

    A handler replaying upstream during teardown raises
    ``TargetClosedError`` into the NEXT test's setup otherwise.

    :param page: Playwright page fixture.
    :returns: Iterator yielding once, then unrouting.
    """
    yield
    page.unroute_all(behavior="ignoreErrors")


def _patch_sandbox_view(page: Page, session_id: str) -> None:
    """Patch the browser's view of *session_id* into a sandbox-hosted shape.

    Registered before navigation: three HTTP route patches plus one WS block
    (the ``test_host_badge.py`` pattern). The snapshot route is registered
    last so it wins for ``/v1/sessions/{id}``.

    :param page: Playwright page, before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :returns: None.
    """
    sandbox_host = {
        "host_id": _SANDBOX_HOST_ID,
        "name": "managed-sandbox",
        "owner": "e2e",
        "status": "online",
        # Non-null marks a server-managed sandbox host; "lakebox" labels it
        # "Databricks Sandbox" in the UI (see sandboxOptionLabel).
        "sandbox_provider": "lakebox",
        "configured_harnesses": None,
        "gateway_inference": None,
    }

    def _patch_hosts(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/v1/hosts":
            route.continue_()
            return
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"hosts": [sandbox_host]}),
        )

    def _patch_health(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/health":
            route.continue_()
            return
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

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["host_id"] = _SANDBOX_HOST_ID
        payload["workspace"] = _SANDBOX_WORKSPACE
        payload["host_resumable"] = True
        payload["created_at"] = _OLD_CREATED_AT
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route(re.compile(r"/v1/hosts(\?|$)"), _patch_hosts)
    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)


def test_fork_sandbox_session_offers_launch_target(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A sandbox-hosted session's fork dialog must offer a usable launch target.

    Journey (the managed-deployment user's path):

    1. Open a session running on a managed sandbox host (its host badge
       resolves to "Databricks Sandbox"); no personal hosts are connected.
    2. Hover the assistant reply → the per-message "Fork from here" action
       (the SPA's only fork entry point) is present — click it.
    3. The fork dialog opens for a coding source, so it asks for a host +
       directory to start the clone on.

    Failure this guards (the live bug): the host picker excludes sandbox
    hosts entirely, so with no personal host connected the dialog dead-ends
    at "No hosts connected yet. Connect one from your terminal:" and the
    submit button never enables — the sandbox session cannot be forked at
    all. Fixed behavior: the dialog offers a launch target for the
    sandbox-hosted source (no connect-instructions dead-end, a host picker,
    and a submittable form once the source prefill lands).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a real
        runner-bound ``hello_world`` session; the browser's view of it is
        patched into the sandbox-hosted shape.
    :returns: None.
    """
    base_url, session_id = seeded_session

    # A settled exchange for the per-message fork action to anchor on —
    # seeded straight into the store so the test doesn't drive the mock LLM.
    seed_committed_turn(
        session_id,
        prompt="Summarize this repo.",
        reply="This repo contains a demo project.",
        response_id="resp_fork_sandbox",
    )

    _patch_sandbox_view(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")

    # The session reads as a sandbox session: the composer's host badge
    # resolves the bound host to its sandbox-provider label.
    badge = page.get_by_test_id("host-badge")
    expect(badge).to_be_visible(timeout=30_000)
    expect(badge).to_contain_text("Databricks Sandbox")

    # Step 2 — the per-message fork action exists on the assistant bubble.
    bubble = page.locator(_ASSISTANT).first
    expect(bubble).to_be_visible(timeout=30_000)
    bubble.hover()
    fork_action = bubble.get_by_test_id("fork-from-response")
    expect(fork_action).to_be_visible()
    fork_action.click()

    # Step 3 — the fork dialog opens for a coding source (host + directory).
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()
    # Let the host query settle so the assertions below judge the loaded
    # state, not the "Loading hosts…" placeholder.
    expect(dialog.get_by_text("Loading hosts…")).to_have_count(0, timeout=15_000)

    # THE BUG: the picker filters out sandbox hosts, so the dialog falls
    # into the no-hosts dead-end even though the source's own sandbox host
    # is online — and the fork can never be submitted.
    expect(dialog.get_by_text("No hosts connected yet")).to_have_count(0)
    expect(page.get_by_test_id("fork-session-host-select")).to_be_visible()
    expect(page.get_by_test_id("fork-session-submit")).to_be_enabled(timeout=15_000)
