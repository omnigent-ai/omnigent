"""E2E: a dormant resumable managed host keeps the composer open.

When a session is bound to a managed host whose sandbox idle-stopped, the
open-session view must NOT dead-end on the ``host_offline`` reconnect banner:
the host is resumable, so the composer stays ENABLED with the normal send
placeholder (the next message resumes the sandbox host). This drives the
``host_asleep`` liveness variant (see ``web/src/hooks/useSessionLiveness.ts``
row 3) end to end — host-bound + ``host_online=false`` + ``host_resumable=true``
+ the runner offline, and outside the startup grace.

The server fixture seeds a normal runner-bound ``hello_world`` session and
``force_host_asleep`` patches the browser's view into the ``host_asleep`` shape
of an Arclet sandbox host.

The same view also offers **Reconnect host**, which asks the real server to
wake the host with a ``retry_session`` event instead of sending a message. The
wake's progress shows only in the composer's host status dot, which spins until
the host is online; there is no toast unless the wake fails.
"""

from __future__ import annotations

import json
import os

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.sessions._host_asleep import (
    force_host_asleep,
    hold_wake_requests,
    next_held_wake,
)

_COMPOSER_PLACEHOLDER = "Send a message…"
# Keeps each status visible long enough to read, only when recording.
_HOLD_MS = 1_500 if os.environ.get("OMNIGENT_E2E_RECORD_DIR") else 0


def _expect_host_status(page: Page, status: str, *, tooltip: str) -> None:
    """Hover the composer's host chip and check its dot and tooltip.

    :param page: Page showing the session composer.
    :param status: Expected ``data-status``, e.g. ``"starting"``.
    :param tooltip: Text the hover tooltip's Status row should show.
    """
    expect(page.get_by_test_id("composer-host-status")).to_have_attribute(
        "data-status", status, timeout=15_000
    )
    page.mouse.move(0, 0)
    page.get_by_test_id("composer-host-select").hover()
    expect(page.get_by_test_id("composer-host-tooltip")).to_contain_text(tooltip, timeout=5_000)
    page.wait_for_timeout(_HOLD_MS)
    page.mouse.move(0, 0)


def _open_host_menu(page: Page, status: str) -> None:
    """Open the host menu and check its current-host dot matches the chip.

    :param page: Page showing the session composer.
    :param status: Expected ``data-status`` for both dots.
    """
    # A trigger click during the previous menu's close animation is dropped.
    expect(page.get_by_role("menu")).to_have_count(0, timeout=5_000)
    page.get_by_test_id("composer-host-select").click()
    expect(page.get_by_role("menu")).to_be_visible(timeout=5_000)
    menu_dot = page.get_by_test_id("composer-host-menu-status")
    expect(menu_dot).to_have_attribute("data-status", status, timeout=5_000)
    chip_class = page.get_by_test_id("composer-host-status").get_attribute("class")
    assert menu_dot.get_attribute("class") == chip_class
    page.wait_for_timeout(_HOLD_MS)


def test_host_asleep_keeps_composer_open(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """In ``host_asleep`` the composer stays enabled with the send placeholder.

    :param request: Supplies the recorded ``page`` after server setup.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to the host_asleep shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    page: Page = request.getfixturevalue("page")
    force_host_asleep(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=15_000)
    # Key behavior: a resumable dormant host keeps the composer open and
    # enabled with the normal send placeholder — NOT the host_offline
    # "Session offline — reconnect" disabled dead-end.
    expect(composer).to_have_attribute("placeholder", _COMPOSER_PLACEHOLDER, timeout=15_000)
    expect(composer).not_to_be_disabled()


def test_reconnect_host_wakes_asleep_sandbox_in_status_dot(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """Reconnect host spins the host dot, then turns it green, with no toast.

    The wake request reaches the real server, which answers once the runner is
    connected; the test holds it so the starting state can be observed, then
    reports the host online before releasing it.

    :param request: Supplies the recorded ``page`` after server setup.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to the host_asleep shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    page: Page = request.getfixturevalue("page")
    host_online = {"online": False}
    force_host_asleep(page, session_id, host_online=host_online)
    held = hold_wake_requests(page, session_id, event_type="retry_session")
    toasts = page.locator("[data-sonner-toast]")

    page.goto(f"{base_url}/c/{session_id}")

    _expect_host_status(page, "offline", tooltip="Asleep — wakes on next message")
    _open_host_menu(page, "offline")
    page.get_by_role("menuitem", name="Reconnect host").click()
    wake = next_held_wake(page, held)
    assert wake.request.post_data_json == {"type": "retry_session", "data": {}}

    _expect_host_status(page, "starting", tooltip="Starting")
    _open_host_menu(page, "starting")
    expect(page.get_by_role("menuitem", name="Reconnect host")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(toasts).to_have_count(0)

    host_online["online"] = True
    wake.continue_()
    _expect_host_status(page, "online", tooltip="Connected")
    _open_host_menu(page, "online")
    page.keyboard.press("Escape")
    expect(toasts).to_have_count(0)


def test_failed_wake_restores_dot_and_reports_error(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """A failed wake stops the spinner, explains why, and offers Reconnect again.

    :param request: Supplies the recorded ``page`` after server setup.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to the host_asleep shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    page: Page = request.getfixturevalue("page")
    force_host_asleep(page, session_id)
    held = hold_wake_requests(page, session_id, event_type="retry_session")

    page.goto(f"{base_url}/c/{session_id}")

    _expect_host_status(page, "offline", tooltip="Asleep — wakes on next message")
    _open_host_menu(page, "offline")
    page.get_by_role("menuitem", name="Reconnect host").click()
    wake = next_held_wake(page, held)
    _expect_host_status(page, "starting", tooltip="Starting")

    wake.fulfill(
        status=502,
        content_type="application/json",
        body=json.dumps(
            {"error": {"code": "runner_unavailable", "message": "Arclet sandbox didn't start."}}
        ),
    )
    expect(
        page.get_by_text("Couldn't reconnect the sandbox: Arclet sandbox didn't start.")
    ).to_be_visible(timeout=15_000)
    _expect_host_status(page, "offline", tooltip="Asleep — wakes on next message")
    _open_host_menu(page, "offline")
    expect(page.get_by_role("menuitem", name="Reconnect host")).to_be_visible()
    page.wait_for_timeout(_HOLD_MS)
    page.keyboard.press("Escape")


def test_session_opened_mid_wake_spins_dot_through_server_stage(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """Opening a session while its sandbox wakes shows the stage in the dot.

    The snapshot reports the server's ``provisioning`` stage, as it does when a
    wake started from another tab or a sent message is still running.

    :param request: Supplies the recorded ``page`` after server setup.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to the host_asleep shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    page: Page = request.getfixturevalue("page")
    host_online = {"online": False}
    force_host_asleep(
        page,
        session_id,
        host_online=host_online,
        sandbox_status={"stage": "provisioning", "provider": "arclet", "error": None},
    )

    page.goto(f"{base_url}/c/{session_id}")

    _expect_host_status(page, "starting", tooltip="Provisioning sandbox")
    _open_host_menu(page, "starting")
    # The server's stage can go stale, so Reconnect host stays available.
    expect(page.get_by_role("menuitem", name="Reconnect host")).to_be_visible()
    page.wait_for_timeout(_HOLD_MS)
    page.keyboard.press("Escape")

    # The next liveness poll (every 10s) sees the host online.
    host_online["online"] = True
    _expect_host_status(page, "online", tooltip="Connected")


def test_wake_stuck_on_stage_brings_reconnect_host_back(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """A wake stuck on launch progress lets Reconnect host return, with no toast.

    The server keeps reporting ``provisioning`` and never answers the wake, as
    when an Arclet box never dials in. The page clock jumps past the wake's
    stage limit instead of waiting it out.

    :param request: Supplies the recorded ``page`` after server setup.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to the host_asleep shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    page: Page = request.getfixturevalue("page")
    force_host_asleep(
        page,
        session_id,
        sandbox_status={"stage": "provisioning", "provider": "arclet", "error": None},
    )
    held = hold_wake_requests(page, session_id, event_type="retry_session")
    toasts = page.locator("[data-sonner-toast]")
    page.clock.install()

    page.goto(f"{base_url}/c/{session_id}")

    _expect_host_status(page, "starting", tooltip="Provisioning sandbox")
    _open_host_menu(page, "starting")
    page.get_by_role("menuitem", name="Reconnect host").click()
    wake = next_held_wake(page, held)
    _open_host_menu(page, "starting")
    expect(page.get_by_role("menuitem", name="Reconnect host")).to_have_count(0)
    page.keyboard.press("Escape")

    page.clock.fast_forward("05:01")
    _expect_host_status(page, "starting", tooltip="Provisioning sandbox")
    _open_host_menu(page, "starting")
    expect(page.get_by_role("menuitem", name="Reconnect host")).to_be_visible()
    page.wait_for_timeout(_HOLD_MS)
    page.keyboard.press("Escape")
    expect(toasts).to_have_count(0)

    # The released wake's late answer doesn't start it again: Reconnect host stays.
    wake.fulfill(status=200, json={"queued": False, "recovered": True})
    _expect_host_status(page, "starting", tooltip="Provisioning sandbox")
    _open_host_menu(page, "starting")
    expect(page.get_by_role("menuitem", name="Reconnect host")).to_be_visible()
    page.keyboard.press("Escape")
    expect(toasts).to_have_count(0)
