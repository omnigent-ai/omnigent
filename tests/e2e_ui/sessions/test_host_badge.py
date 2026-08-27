"""E2E: the composer's host badge shows which host a session is bound to.

The badge (``web/src/components/HostBadge.tsx``) renders in the composer's
status-line tray for a host-bound session: the friendly host name (or a
sandbox-provider label) plus a green/red status dot driven by the live host
``host_online`` signal. It renders nothing when the session isn't host-bound.

A DISCONNECTED host keeps that exact shape — name + red dot — and becomes
clickable, opening the reconnect instructions. That one shape is the point: the
badge must not swap the host's name for generic "Host is offline" copy in some
disconnect states and keep it in others, which is what it used to do depending
on whether the runner happened to outlive the host (see the reconnect tests
below). A dormant *resumable* managed host is the deliberate exception: its
offline is idle dormancy the next message wakes, so it gets no reconnect
affordance.

Clicking a badge that has nothing to reconnect opens ``SwitchHostDialog``
instead, which moves the session to another machine — covered at the bottom of
this module.

The harness seeds a normal runner-bound ``hello_world`` session, so the browser
view is patched into a host-bound shape via route interception (same approach as
``test_host_asleep_composer.py``):

- ``GET /v1/sessions/{id}`` (snapshot) → ``host_id`` set so the badge has a host
  to resolve, plus ``host_resumable`` and an old ``created_at`` (past the
  ``STARTING_GRACE_S`` startup window).
- ``GET /v1/hosts`` → returns the bound host so the badge resolves its name (or
  the sandbox-provider label). ``HostBadge`` calls this via
  ``useHosts({ includeSandbox: true })``.
- ``GET /health`` → reports the session's live ``runner_online`` /
  ``host_online`` so the dot reads online/offline; the open-session poll
  overrides the WS stream.
- ``GET /v1/sessions`` (sidebar list) → the session is dropped so the open row
  resolves off-sidebar from the patched snapshot (host-bound).
- ``WS /v1/sessions/updates`` → blocked so a stream push can't revert liveness to
  the real (runner-online, host-unbound) values.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

_FAKE_HOST_ID = "host_test_badge"
# Unix seconds well before now so an offline host is outside the startup grace
# (STARTING_GRACE_S) and reads its real liveness, not `starting` — see
# useSessionLiveness. Harmless for the online case.
_OLD_CREATED_AT = 1_700_000_000


@pytest.fixture(autouse=True)
def _drop_routes(page: Page) -> Iterator[None]:
    """Drop this module's route handlers before the page closes.

    Every test here leaves a ``/health`` poll and snapshot refetches in flight.
    A handler that replays upstream as the page tears down raises
    ``TargetClosedError``, which surfaces as an error in the NEXT test's setup.

    :param page: Playwright page fixture.
    :returns: Iterator yielding once, then unrouting.
    """
    yield
    page.unroute_all(behavior="ignoreErrors")


def _patch_host_view(
    page: Page,
    session_id: str,
    *,
    host: dict,
    host_online: bool,
    runner_online: bool = False,
    host_resumable: bool = True,
) -> None:
    """Patch the browser's view of ``session_id`` into a host-bound shape.

    Registered before navigation: four HTTP route patches plus one WS block.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :param host: The host record ``GET /v1/hosts`` should return for the bound
        host (``host_id`` is overwritten to match the patched snapshot).
    :param host_online: Live ``host_online`` the session reports via ``/health``;
        drives the badge's online (green) vs offline (red) dot.
    :param runner_online: Live ``runner_online`` the session reports via
        ``/health``. With the host down, ``True`` is the runner-outlives-host
        case (liveness stays ``online``) and ``False`` is the fully-unreachable
        one (``host_offline``) — the two states that used to render different
        host indicators.
    :param host_resumable: Whether the bound host is a resumable managed host.
        ``True`` (the default) keeps an offline host out of the ``host_offline``
        dead-end so the chat view renders normally; ``False`` is a real
        laptop/external host the user reconnects with ``omnigent host``.
    """
    host = {**host, "host_id": _FAKE_HOST_ID}

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["host_id"] = _FAKE_HOST_ID
        payload["host_resumable"] = host_resumable
        payload["created_at"] = _OLD_CREATED_AT
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_hosts(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/v1/hosts":
            route.continue_()
            return
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"hosts": [host]}),
        )

    def _patch_list(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/v1/sessions":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            payload["data"] = [
                r for r in rows if not (isinstance(r, dict) and r.get("id") == session_id)
            ]
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
        response = fetch_with_retry(route)
        payload = response.json()
        live = {"runner_online": runner_online, "host_online": host_online}
        if isinstance(payload.get("sessions"), dict):
            payload["sessions"][session_id] = live
        if isinstance(payload.get("session"), dict):
            payload["session"] = {**payload["session"], **live}
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    # Snapshot route registered last so it wins for /v1/sessions/{id} (Playwright
    # matches most-recently-registered first); the others fall through via
    # continue_() for anything they don't own.
    page.route(re.compile(r"/v1/hosts(\?|$)"), _patch_hosts)
    page.route(re.compile(r"/v1/sessions(\?|$)"), _patch_list)
    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)


def test_host_badge_shows_host_name_when_online(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A host-bound, online session shows the host name with an online status.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to a host-bound, online shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_host_view(
        page,
        session_id,
        host={"name": "e2e-host", "owner": "e2e", "status": "online", "sandbox_provider": None},
        host_online=True,
    )

    page.goto(f"{base_url}/c/{session_id}")

    badge = page.get_by_test_id("host-badge")
    expect(badge).to_be_visible(timeout=15_000)
    expect(badge).to_contain_text("e2e-host")
    # The dot is decorative; status is conveyed by the title (mouse hover) and an
    # sr-only word. Online == reachable host. The title also advertises the
    # switch affordance the badge carries for any non-sandbox host.
    expect(badge).to_have_attribute(
        "title", "Host e2e-host, online — click to switch", timeout=15_000
    )


def test_host_badge_shows_offline_when_host_unreachable(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A dormant resumable managed host shows offline, and stays passive.

    ``host_resumable`` is the one offline host with no reconnect affordance: the
    sandbox is idle-stopped and the next message wakes it, so ``omnigent host``
    would be the wrong instruction. Every other offline host is clickable (see
    the reconnect tests below).

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to a host-bound, offline shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_host_view(
        page,
        session_id,
        host={"name": "e2e-host", "owner": "e2e", "status": "offline", "sandbox_provider": None},
        host_online=False,
        host_resumable=True,
    )

    page.goto(f"{base_url}/c/{session_id}")

    badge = page.get_by_test_id("host-badge")
    expect(badge).to_be_visible(timeout=15_000)
    expect(badge).to_contain_text("e2e-host")
    # No reconnect copy: `omnigent host` is the wrong instruction for a dormant
    # sandbox. The badge is still clickable — that click switches hosts — so the
    # absence of the reconnect affordance is what this pins.
    expect(badge).to_have_attribute(
        "title", "Host e2e-host, offline — click to switch", timeout=15_000
    )
    expect(badge).not_to_contain_text("click to reconnect")


def test_host_badge_offline_host_keeps_name_while_runner_outlives_it(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A host that drops while its runner keeps streaming is still reconnectable.

    Liveness still calls this session ``online`` (the runner tunnel is up), so
    the badge used to render a passive name + red dot with no way to act on it.
    It now carries the same reconnect affordance as any other dropped host.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to host-down + runner-up.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_host_view(
        page,
        session_id,
        host={"name": "e2e-host", "owner": "e2e", "status": "offline", "sandbox_provider": None},
        host_online=False,
        runner_online=True,
        host_resumable=False,
    )

    page.goto(f"{base_url}/c/{session_id}")

    badge = page.get_by_test_id("host-badge")
    expect(badge).to_be_visible(timeout=15_000)
    expect(badge).to_contain_text("e2e-host")
    expect(badge).to_have_attribute(
        "title", "Host e2e-host, offline — click to reconnect", timeout=15_000
    )
    assert badge.evaluate("el => el.tagName") == "BUTTON"
    # The runner is alive, so the session itself is still usable — the offline
    # host must not block the composer here.
    expect(page.get_by_label("Message the agent")).not_to_be_disabled()


def test_host_badge_offline_host_keeps_name_when_runner_is_down_too(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A fully unreachable session names its dropped host instead of generic copy.

    This is the ``host_offline`` liveness state, which used to replace the host
    name with "Host is offline — click to reconnect". The name is what tells the
    user WHICH machine to go restart, so it stays; the separate band below the
    composer stays suppressed.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to host-down + runner-down.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_host_view(
        page,
        session_id,
        host={"name": "e2e-host", "owner": "e2e", "status": "offline", "sandbox_provider": None},
        host_online=False,
        runner_online=False,
        host_resumable=False,
    )

    page.goto(f"{base_url}/c/{session_id}")

    badge = page.get_by_test_id("host-badge")
    expect(badge).to_be_visible(timeout=15_000)
    expect(badge).to_contain_text("e2e-host")
    expect(badge).to_have_attribute(
        "title", "Host e2e-host, offline — click to reconnect", timeout=15_000
    )
    assert badge.evaluate("el => el.tagName") == "BUTTON"
    # The badge owns the affordance; the old band below the composer stays away.
    expect(page.get_by_test_id("disconnected-indicator")).to_have_count(0)


def test_host_badge_click_shows_host_reconnect_instructions(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Clicking the offline host badge opens the host reconnect instructions.

    Driven from the runner-still-up case: liveness reads ``online`` there, so
    the dialog's state must come from the session's host binding — otherwise it
    would hand out the local ``omnigent run --resume`` command for a session
    whose host is what actually needs restarting.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to host-down + runner-up.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_host_view(
        page,
        session_id,
        host={"name": "e2e-host", "owner": "e2e", "status": "offline", "sandbox_provider": None},
        host_online=False,
        runner_online=True,
        host_resumable=False,
    )

    page.goto(f"{base_url}/c/{session_id}")

    badge = page.get_by_test_id("host-badge")
    expect(badge).to_be_visible(timeout=15_000)
    badge.click()

    dialog = page.get_by_test_id("reconnect-session-dialog")
    expect(dialog).to_be_visible(timeout=15_000)
    expect(dialog).to_contain_text("Host is offline")
    page.get_by_test_id("reconnect-session-tab-reconnect").click()
    expect(page.get_by_test_id("reconnect-session-description")).to_contain_text(
        "This session's host"
    )
    expect(page.get_by_test_id("reconnect-session-command")).to_contain_text("omnigent host")


def test_host_badge_labels_sandbox_session_by_provider(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A sandbox-backed session is labeled by its provider, not its managed name.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to a sandbox-host-bound shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_host_view(
        page,
        session_id,
        host={
            "name": "managed-cd8f66d0",
            "owner": "e2e",
            "status": "online",
            "sandbox_provider": "databricks-lakebox",
        },
        host_online=True,
    )

    page.goto(f"{base_url}/c/{session_id}")

    badge = page.get_by_test_id("host-badge")
    expect(badge).to_be_visible(timeout=15_000)
    # The managed-<hex> host name is collapsed to the provider label.
    expect(badge).to_contain_text("Databricks-lakebox Sandbox")
    expect(badge).not_to_contain_text("managed-cd8f66d0")


def _patch_switch_targets(page: Page, session_id: str, *, target_host_id: str) -> list[dict]:
    """Offer a second online host and capture the two calls a switch makes.

    The move has no dedicated endpoint — it is a ``PATCH /v1/sessions/{id}``
    that releases the runner binding followed by a
    ``POST /v1/hosts/{target}/runners`` that binds a new one. Both are
    intercepted and answered here so the test never needs a second real host;
    what it asserts is that the UI issues them, in that order, with the right
    payloads.

    :param page: Playwright page before navigation.
    :param target_host_id: Host id the dialog should offer as a move target.
    :returns: A list that accumulates ``{"kind", "body"}`` for each captured
        call, in the order the browser made them.
    """
    calls: list[dict] = []

    def _patch_release(route: Route) -> None:
        request = route.request
        if request.method != "PATCH":
            # fallback(), not continue_(): this route shadows the snapshot patch
            # registered earlier, and continue_() would skip it and hit the
            # network with the unpatched (host-unbound) session.
            route.fallback()
            return
        calls.append({"kind": "release", "body": request.post_data_json})
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"id": session_id, "runner_id": None, "model_override": None}),
        )

    def _patch_launch(route: Route) -> None:
        calls.append({"kind": "launch", "body": route.request.post_data_json})
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"runner_id": "runner_switched"}),
        )

    def _patch_filesystem(route: Route) -> None:
        # The dialog lists the target's home to default the directory.
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {"entries": [{"path": "/home/e2e/repo", "name": "repo", "is_dir": True}]}
            ),
        )

    page.route(re.compile(r"/v1/hosts/[^/]+/filesystem"), _patch_filesystem)
    page.route(re.compile(rf"/v1/hosts/{re.escape(target_host_id)}/runners(\?|$)"), _patch_launch)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_release)
    return calls


def test_host_badge_switches_the_session_to_another_host(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Clicking an online host badge moves the session to another machine.

    The whole point of the flow: pick a host, pick a directory, and the session
    is released from the old runner and relaunched on the new host. The origin
    host is excluded from the picker (moving to where it already is is a no-op).

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to a host-bound, online shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    target_id = "host_test_target"
    _patch_host_view(
        page,
        session_id,
        host={"name": "e2e-host", "owner": "e2e", "status": "online", "sandbox_provider": None},
        host_online=True,
    )

    # Re-patch /v1/hosts to advertise BOTH the bound host and a move target.
    def _patch_two_hosts(route: Route) -> None:
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
                            "name": "e2e-host",
                            "owner": "e2e",
                            "status": "online",
                            "sandbox_provider": None,
                        },
                        {
                            "host_id": target_id,
                            "name": "e2e-target",
                            "owner": "e2e",
                            "status": "online",
                            "sandbox_provider": None,
                        },
                    ]
                }
            ),
        )

    page.route(re.compile(r"/v1/hosts(\?|$)"), _patch_two_hosts)
    calls = _patch_switch_targets(page, session_id, target_host_id=target_id)

    page.goto(f"{base_url}/c/{session_id}")

    badge = page.get_by_test_id("host-badge")
    expect(badge).to_be_visible(timeout=15_000)
    badge.click()

    dialog = page.get_by_test_id("switch-host-dialog")
    expect(dialog).to_be_visible(timeout=15_000)
    # The only other online host is preselected, so the common "just move it
    # over there" case is one click.
    trigger = page.get_by_test_id("switch-host-select")
    expect(trigger).to_contain_text("e2e-target", timeout=15_000)
    # The preselection is itself the "origin is excluded" signal: the bound host
    # is the only other one in the list, so it landing on e2e-target means it
    # was filtered out. Which hosts the picker offers is pinned exactly (both
    # normally and after a stranded failure) in SwitchHostDialog.test.tsx — this
    # test deliberately does NOT open the dropdown, because Radix's dismissable
    # layer leaves the page briefly unable to receive the submit click.
    expect(trigger).not_to_contain_text("e2e-host")

    directory = page.get_by_test_id("workspace-path-input")
    expect(directory).to_be_visible(timeout=15_000)
    directory.fill("/home/e2e/repo")
    # Dismiss the path field's suggestion dropdown and wait for it to go before
    # submitting. It is rendered in flow, so closing it lifts the footer ~24px:
    # a mousedown on the submit button closes the dropdown, the button moves out
    # from under the pointer, and the mouseup never lands on it.
    page.get_by_test_id("switch-host-dialog").get_by_text("Switch host", exact=True).first.click()
    expect(page.get_by_test_id("workspace-path-dropdown")).to_have_count(0)

    switch = page.get_by_test_id("switch-host-button")
    expect(switch).to_be_enabled(timeout=15_000)
    switch.click()

    # The dialog closes on success, and both legs of the move went out.
    expect(dialog).to_have_count(0, timeout=15_000)
    kinds = [c["kind"] for c in calls]
    assert kinds == ["release", "launch"], f"expected release then launch, got {kinds}"
    release_body = calls[0]["body"] or {}
    # The runner binding is released, and the model override goes with it: a
    # model id is resolved against the OLD host's catalog.
    assert release_body.get("runner_id") == "", release_body
    assert release_body.get("model_override") == "default", release_body
    launch_body = calls[1]["body"] or {}
    assert launch_body.get("session_id") == session_id, launch_body
    assert launch_body.get("workspace") == "/home/e2e/repo", launch_body
