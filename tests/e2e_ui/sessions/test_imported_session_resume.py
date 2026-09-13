"""E2E: resume a real imported / host-less session onto a chosen online host.

An imported session has no host binding (``host_id`` null) and no runner, so it
used to read as a normal reachable session (or, with no executor, a terminal
reconnect dead-end). Now the server reports an unbound imported transcript as
``runner_online: false`` (it has no live executor anywhere), and the open view
routes to the in-app "Resume on a machine" picker — the same bind + launch path
(``POST /v1/hosts/{id}/runners``) the fork-resume dialog uses.

This drives the REAL liveness path: it imports a genuine transcript through the
public ``POST /v1/imports`` API and never stubs ``/health`` or the session
snapshot. The only stubs are the host-side wire the harness can't provide (it
spawns no ``omnigent host`` daemon): the hosts list reports one online host, the
filesystem pre-flight lists empty, and the runner launch records its body.
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

_HOST_ID = "host_imported_e2e"
_WS_DIR = "/home/e2e/imported-repo"


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_imported_session_resumes_onto_chosen_local_host(
    page: Page,
    live_server: str,
) -> None:
    """A real imported session offers the resume picker and launches a runner.

    Failure modes this guards:

    - The server stops reporting an unbound import as ``runner_online: false``
      (the ``imported`` connectivity marker), so it reads as reachable and the
      picker never appears.
    - The client re-applies the cold-boot startup grace to imports, so the
      picker is masked behind "Connecting…" instead of showing at once.
    - The offline routing regresses to the terminal reconnect dead-end instead
      of the picker.
    - The bind wire shape drifts (wrong session_id / workspace on the launch).

    :param page: Playwright page fixture (fresh context per test).
    :param live_server: Base URL of the spawned server.
    """
    base_url = live_server

    runner_bodies: list[dict[str, Any]] = []

    def handle_hosts(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "hosts": [
                        {
                            "host_id": _HOST_ID,
                            "name": "e2e-laptop",
                            "owner": "e2e",
                            "status": "online",
                            "sandbox_provider": None,
                            "configured_harnesses": {},
                        }
                    ]
                }
            ),
        )

    def handle_filesystem(route: Route) -> None:
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
            body=json.dumps({"runner_id": "runner_imported_e2e", "status": "launching"}),
        )

    page.route("**/v1/hosts", handle_hosts)
    page.route(f"**/v1/hosts/{_HOST_ID}/filesystem/**", handle_filesystem)
    page.route(f"**/v1/hosts/{_HOST_ID}/runners", handle_runners)

    # Import a genuine transcript through the public API. The route stamps the
    # ``omnigent.import.source`` label and leaves the session host-less — the
    # exact state a real ``omnigent import`` produces.
    resp = httpx.post(
        f"{base_url}/v1/imports",
        json={
            "source": "claude",
            "external_session_id": f"e2e-import-{uuid4().hex}",
            "workspace": _WS_DIR,
            "items": [
                {
                    "type": "message",
                    "response_id": "resp_1",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello from an import"}],
                    },
                }
            ],
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    session_id = resp.json()["session_id"]

    page.goto(f"{base_url}/c/{session_id}")

    # Real liveness resolves the import to local_stranded (server reports
    # runner_online=false; the client skips the cold-boot grace for imports),
    # so the disconnected banner shows. Clicking it must open the resume
    # picker, NOT the terminal reconnect dialog.
    indicator = page.get_by_test_id("disconnected-indicator")
    expect(indicator).to_be_visible(timeout=30_000)
    indicator.click()

    dialog = page.get_by_test_id("resume-dir-dialog")
    expect(dialog).to_be_visible()
    expect(page.get_by_test_id("reconnect-session-dialog")).not_to_be_visible()

    # Prefill: the session's own workspace, host defaulted to the sole online
    # machine → the bind button is enabled without any further input.
    bind = page.get_by_test_id("resume-dir-bind-button")
    expect(bind).to_be_enabled(timeout=10_000)
    bind.click()

    deadline = time.monotonic() + 30.0
    while not runner_bodies and time.monotonic() < deadline:
        time.sleep(0.2)
    assert len(runner_bodies) == 1, "expected exactly one runner launch"
    launch = runner_bodies[0]
    assert launch["session_id"] == session_id, launch
    assert launch["workspace"] == _WS_DIR, launch
