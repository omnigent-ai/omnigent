"""Browser coverage for slash-command retry identity and replacement risk."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Page, Request, Route, expect

_COMPOSER_LABEL = "Message the agent"
_SKILL_NAME = "browser-skill"


def _wait_for(page: Page, predicate: Callable[[], bool], *, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label(_COMPOSER_LABEL)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _command_text(event: dict[str, Any]) -> str:
    data = event.get("data")
    assert isinstance(data, dict)
    arguments = data.get("arguments")
    suffix = f" {arguments}" if arguments else ""
    return f"/{data.get('name')}{suffix}"


def test_skill_retries_preserve_identity_and_intentional_repeats_do_not(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Exercise lost responses, edited replacement, and intentional repeats."""
    base_url, session_id = seeded_session
    attempts: list[dict[str, Any]] = []
    invocations: list[dict[str, Any]] = []
    accepted_ids: set[str] = set()
    lost_once: set[str] = set()
    lose_response_for = {
        f"/{_SKILL_NAME} accepted-loss",
        f"/{_SKILL_NAME} edit-original",
    }

    def expose_browser_skill(route: Route, request: Request) -> None:
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        payload["skills"] = [
            {
                "name": _SKILL_NAME,
                "description": "Browser-only identity regression skill",
            }
        ]
        route.fulfill(response=response, json=payload)

    def accept_skill_event(route: Route, request: Request) -> None:
        body = request.post_data_json
        if (
            request.method != "POST"
            or not isinstance(body, dict)
            or body.get("type") != "slash_command"
        ):
            route.continue_()
            return
        attempts.append(body)
        command = _command_text(body)
        client_event_id = body.get("client_event_id")
        assert isinstance(client_event_id, str)
        replayed = client_event_id in accepted_ids
        if not replayed:
            accepted_ids.add(client_event_id)
            invocations.append(body)
        if command in lose_response_for and command not in lost_once:
            lost_once.add(command)
            route.abort("failed")
            return
        route.fulfill(
            status=202,
            json={"queued": True, "idempotency_replayed": replayed},
        )

    page.route(f"**/v1/sessions/{session_id}*", expose_browser_skill)
    page.route(f"**/v1/sessions/{session_id}/events", accept_skill_event)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)

    # The backend accepts the skill, but the browser loses the response.
    # Restoring and retrying unchanged must keep one identity and invocation.
    unchanged = f"/{_SKILL_NAME} accepted-loss"
    _send(page, unchanged)
    expect(composer).to_have_value(unchanged, timeout=10_000)
    page.get_by_role("button", name="Send", exact=True).click()
    _wait_for(page, lambda: len(attempts) == 2)
    assert attempts[0]["client_event_id"] == attempts[1]["client_event_id"]
    assert sum(_command_text(event) == unchanged for event in invocations) == 1

    page.reload()
    expect(composer).to_be_visible(timeout=30_000)

    # Editing a restored uncertain skill does not send before confirmation.
    original = f"/{_SKILL_NAME} edit-original"
    replacement = f"/{_SKILL_NAME} edit-replacement"
    _send(page, original)
    expect(composer).to_have_value(original, timeout=10_000)
    composer.fill(replacement)
    before_cancel = len(attempts)
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.get_by_role("button", name="Send", exact=True).click()
    page.wait_for_timeout(200)
    assert len(attempts) == before_cancel

    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Send", exact=True).click()
    _wait_for(page, lambda: len(attempts) == before_cancel + 1)
    assert attempts[-1]["client_event_id"] != attempts[-2]["client_event_id"]
    assert sum(_command_text(event) == replacement for event in invocations) == 1

    page.reload()
    expect(composer).to_be_visible(timeout=30_000)

    # Two separate identical skill actions each receive a fresh identity.
    intentional = f"/{_SKILL_NAME} intentional-repeat"
    _send(page, intentional)
    _wait_for(page, lambda: _command_text(attempts[-1]) == intentional)
    first_intentional_id = attempts[-1]["client_event_id"]
    page.reload()
    expect(composer).to_be_visible(timeout=30_000)
    _send(page, intentional)
    _wait_for(
        page,
        lambda: (
            _command_text(attempts[-1]) == intentional
            and attempts[-1]["client_event_id"] != first_intentional_id
        ),
    )
    assert sum(_command_text(event) == intentional for event in invocations) == 2

    # Keep a compact diagnostic if this ever regresses under browser timing.
    assert len(accepted_ids) == len(invocations), json.dumps(attempts, indent=2)
