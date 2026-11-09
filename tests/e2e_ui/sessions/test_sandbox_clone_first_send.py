"""E2E: a clone whose sandbox is still launching takes its first message.

A sandbox clone remains unbound until its sandbox registers: "Clone & start"
returns as soon as the fork row exists, and the clone has no host or workspace
until then, so it looks like an unbound coding fork. Its first send must go to
the server, which holds it for the launch, not open the resume picker.

The clone is a real fork with the fork-source label a coding source stamps, so
the server reports it as an unbound fork (``needs_workspace``, runner offline).
The harness has no sandbox provider, so two routes stand in for the launch:

- ``GET /v1/sessions/{fork}`` (snapshot) reports ``sandbox_status``
  ``provisioning``, as the fork route seeds it before returning.
- ``POST /v1/sessions/{fork}/events`` is recorded and answered 202, as a server
  holding the message for the launch would.

The second test keeps the same unbound clone without a launch in flight and
checks it still opens the picker, so the first test's pass isn't an artifact of
the patching.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

_MESSAGE = "Earlier I gave you a code word. Reply with it."
_PROVISIONING = {"stage": "provisioning", "provider": "modal", "error": None}
_FORK_SOURCE_LABEL = "omnigent.fork.source_id"


def _fork(base_url: str, source_id: str) -> str:
    """Fork *source_id* into an unbound coding clone and return its id.

    The seeded source has no workspace, so the fork route leaves the
    fork-source label off; set it as a coding source's fork would carry it.

    :param base_url: Spawned server base URL.
    :param source_id: Session to fork, e.g. ``"conv_abc123"``.
    :returns: The clone's session id.
    """
    resp = httpx.post(f"{base_url}/v1/sessions/{source_id}/fork", json={}, timeout=60.0)
    resp.raise_for_status()
    fork_id = str(resp.json()["id"])
    httpx.patch(
        f"{base_url}/v1/sessions/{fork_id}",
        json={"labels": {_FORK_SOURCE_LABEL: source_id}},
        timeout=30.0,
    ).raise_for_status()
    return fork_id


def _open_clone(
    page: Page,
    base_url: str,
    *,
    source_id: str,
    fork_id: str,
    sandbox_status: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Open the unbound clone *fork_id*, reporting *sandbox_status* in its snapshot.

    :param page: Playwright page before navigation.
    :param base_url: Spawned server base URL.
    :param source_id: The clone's source, carried in its fork-source label.
    :param fork_id: The clone to open.
    :param sandbox_status: Launch stage the snapshot reports, or ``None`` for
        no launch in flight.
    :returns: Bodies of the message events the page posts, filled as they arrive.
    """
    posted: list[dict[str, Any]] = []

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{fork_id}":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        assert payload["labels"].get(_FORK_SOURCE_LABEL) == source_id
        assert payload.get("workspace") is None
        payload["sandbox_status"] = sandbox_status
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _record_message(route: Route) -> None:
        request = route.request
        body = json.loads(request.post_data or "{}") if request.method == "POST" else {}
        if body.get("type") != "message":
            route.continue_()
            return
        posted.append(body)
        route.fulfill(status=202, json={"queued": True})

    page.route(re.compile(rf"/v1/sessions/{re.escape(fork_id)}(\?|$)"), _patch_snapshot)
    page.route(re.compile(rf"/v1/sessions/{re.escape(fork_id)}/events(\?|$)"), _record_message)
    page.goto(f"{base_url}/c/{fork_id}")
    return posted


def _send(page: Page) -> None:
    """Type :data:`_MESSAGE` into the composer and send it.

    :param page: Page showing the clone.
    """
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_enabled(timeout=15_000)
    composer.fill(_MESSAGE)
    page.get_by_role("button", name="Send", exact=True).click()


def _wait_for_message(page: Page, posted: list[dict[str, Any]], timeout_ms: int = 10_000) -> None:
    """Wait until the page has posted :data:`_MESSAGE` as a message event.

    Waits on the page (not ``time.sleep``) so Playwright keeps delivering the
    route that fills *posted*.

    :param page: Page whose event loop delivers the route.
    :param posted: List filled by :func:`_open_clone`.
    :param timeout_ms: How long to wait.
    """
    dialog = page.get_by_test_id("resume-dir-dialog")
    deadline = time.monotonic() + timeout_ms / 1000
    while not any(_MESSAGE in json.dumps(body) for body in posted):
        # Fail on the picker as soon as it shows, naming what it told the user.
        assert not dialog.is_visible(), (
            f"the send opened the resume picker: {dialog.inner_text()!r}"
        )
        assert time.monotonic() < deadline, f"the message never reached the server: {posted!r}"
        page.wait_for_timeout(50)


def test_launching_sandbox_clone_sends_first_message(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The first send into a launching sandbox clone reaches the server.

    Failure modes this catches:

    - The send routes to "Resume this session" (the unbound-fork picker), which
      tells the user the source's host is offline while the server is placing
      the clone on its own sandbox.
    - The send is swallowed: no message event reaches the server.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for the clone's source.
    """
    base_url, source_id = seeded_session
    fork_id = _fork(base_url, source_id)
    posted = _open_clone(
        page, base_url, source_id=source_id, fork_id=fork_id, sandbox_status=_PROVISIONING
    )
    expect(page.get_by_text("Provisioning sandbox").first).to_be_visible(timeout=15_000)

    _send(page)

    _wait_for_message(page, posted)
    expect(page.get_by_test_id("resume-dir-dialog")).to_have_count(0)


def test_unbound_clone_without_launch_still_opens_picker(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """With no launch in flight, the same unbound clone opens the resume picker.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for the clone's source.
    """
    base_url, source_id = seeded_session
    fork_id = _fork(base_url, source_id)
    posted = _open_clone(page, base_url, source_id=source_id, fork_id=fork_id, sandbox_status=None)

    _send(page)

    dialog = page.get_by_test_id("resume-dir-dialog")
    expect(dialog).to_be_visible(timeout=10_000)
    expect(dialog).to_contain_text("Resume this session")
    assert not posted, "the picker path must hold the message until the clone is bound"
