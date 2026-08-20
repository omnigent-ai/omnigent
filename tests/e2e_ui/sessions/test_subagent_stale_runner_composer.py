"""E2E: a sub-agent session with a dead runner keeps the composer open.

When a sub-agent's runner dies (idle timeout, crash), the session must NOT
show the "Agent disconnected — click to reconnect" banner or disable the
composer.  Sub-agents have no host binding and can't be relaunched from a CLI
command — they recover via their parent's live runner transparently on message
send (server-side heal, #3151).

Before the fix the liveness hook classified them as ``local_stranded`` (same as
a dead CLI session), which blocked the composer and opened the CLI reconnect
modal.  After the fix ``kind == "sub_agent"`` maps to ``runner_asleep`` instead,
keeping the composer enabled.

The fixture creates a real parent session, then creates a child session with
``parent_session_id`` set (which the server records as ``kind="sub_agent"``).
The browser's view of the child is then patched via route interception to look
like its runner is offline — the same conditions that triggered the bad modal.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

_OLD_CREATED_AT = 1_700_000_000
_RECONNECT_BANNER = "Agent disconnected — click to reconnect"
_OFFLINE_PLACEHOLDER = "Session offline — reconnect below to continue"


def _force_runner_offline(page: Page, session_id: str) -> None:
    """Patch the browser's view of ``session_id`` to look like runner is down.

    The snapshot ``kind`` is preserved as-is (``"sub_agent"``); only runner
    liveness is forced offline and the session is aged past the startup grace.

    :param page: Playwright page before navigation.
    :param session_id: Child session id to patch.
    """

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        # Age the session out of the startup grace so the runner-down signal is
        # not masked as a cold boot.
        payload["created_at"] = _OLD_CREATED_AT
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_health(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/health":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        offline = {"runner_online": False, "host_online": None}
        if isinstance(payload.get("sessions"), dict):
            payload["sessions"][session_id] = offline
        if isinstance(payload.get("session"), dict):
            payload["session"] = {**payload["session"], **offline}
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)


@pytest.mark.e2e_ui
def test_subagent_dead_runner_keeps_composer_open(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A sub-agent session with a dead runner shows no reconnect modal.

    Regression for the bug where ``kind="sub_agent"`` sessions with a dead
    runner classified as ``local_stranded``, showing "Agent disconnected —
    click to reconnect" and disabling the composer.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real parent
        session; a child is created from it.
    :returns: None.
    """
    base_url, parent_session_id = seeded_session

    # Fetch the parent's agent_id so we can create a child session.
    agent_resp = httpx.get(
        f"{base_url}/v1/sessions/{parent_session_id}/agent",
        timeout=10.0,
    )
    agent_resp.raise_for_status()
    agent_id = agent_resp.json()["id"]

    # Create a sub-agent child session.
    child_resp = httpx.post(
        f"{base_url}/v1/sessions",
        json={
            "agent_id": agent_id,
            "parent_session_id": parent_session_id,
            "title": "impl:task-1",
        },
        timeout=10.0,
    )
    child_resp.raise_for_status()
    child_id = child_resp.json()["id"]

    # Patch the child's view to make it appear offline (dead runner, no host).
    _force_runner_offline(page, child_id)

    page.goto(f"{base_url}/c/{child_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=15_000)

    # The composer must be enabled — sub-agents recover via their parent's
    # runner and must not be blocked by the reconnect modal.
    expect(composer).not_to_be_disabled()

    # The offline placeholder must NOT appear — that text means local_stranded.
    expect(composer).not_to_have_attribute("placeholder", _OFFLINE_PLACEHOLDER, timeout=5_000)

    # The "Agent disconnected — click to reconnect" banner must NOT appear.
    expect(page.get_by_text(_RECONNECT_BANNER)).not_to_be_visible(timeout=5_000)
