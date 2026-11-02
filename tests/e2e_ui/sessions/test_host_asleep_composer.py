"""E2E: a dormant resumable managed host keeps the composer open.

When a session is bound to a managed host whose sandbox idle-stopped, the
open-session view must NOT dead-end on the ``host_offline`` reconnect banner:
the host is resumable, so the composer stays ENABLED with the normal send
placeholder (the next message resumes the sandbox host). This drives the
``host_asleep`` liveness variant (see ``web/src/hooks/useSessionLiveness.ts``
row 3) end to end — host-bound + ``host_online=false`` + ``host_resumable=true``
+ the runner offline, and outside the startup grace.

The server fixture seeds a normal runner-bound ``hello_world`` session and
``force_host_asleep`` patches the browser's view into the ``host_asleep`` shape.

The same view also offers **Reconnect host**, which asks the real server to
wake the host with a ``retry_session`` event instead of sending a message.
"""

from __future__ import annotations

from urllib.parse import urlparse

from playwright.sync_api import Page, expect

from tests.e2e_ui.sessions._host_asleep import force_host_asleep

_COMPOSER_PLACEHOLDER = "Send a message…"


def test_host_asleep_keeps_composer_open(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """In ``host_asleep`` the composer stays enabled with the send placeholder.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to the host_asleep shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    force_host_asleep(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=15_000)
    # Key behavior: a resumable dormant host keeps the composer open and
    # enabled with the normal send placeholder — NOT the host_offline
    # "Session offline — reconnect" disabled dead-end.
    expect(composer).to_have_attribute("placeholder", _COMPOSER_PLACEHOLDER, timeout=15_000)
    expect(composer).not_to_be_disabled()


def test_reconnect_host_asks_server_to_wake_asleep_host(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Reconnect host sends ``retry_session`` and reports the server's answer.

    The seeded session's runner is really connected, so the real server answers
    ``already_connected`` and the toast says the sandbox is already online.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to the host_asleep shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    force_host_asleep(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    page.get_by_test_id("composer-host-select").click()
    with page.expect_request(
        lambda request: (
            request.method == "POST"
            and urlparse(request.url).path == f"/v1/sessions/{session_id}/events"
            and "retry_session" in (request.post_data or "")
        )
    ) as wake:
        page.get_by_role("menuitem", name="Reconnect host").click()
    assert wake.value.post_data_json == {"type": "retry_session", "data": {}}
    expect(page.get_by_text("Sandbox is already online.")).to_be_visible(timeout=15_000)
