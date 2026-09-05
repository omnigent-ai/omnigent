"""E2E: codex-native Approvals picker switches the live approval mode."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

# Conversation-label key the server stamps after a confirmed switch.
_APPROVAL_MODE_LABEL_KEY = "omnigent.codex_native.approval_mode"


def _patch_session_as_codex_native(
    page: Page,
    session_id: str,
    *,
    approval_mode: str | None = None,
) -> list[dict]:
    """Patch the browser's session snapshot into a codex-native response.

    The server fixture seeds a normal ``hello_world`` session so the page can
    boot against the real app/server. This route patch changes only ``GET``
    and ``PATCH /v1/sessions/{session_id}`` responses as seen by the browser,
    simulating a codex-native session. An ``approval_mode`` PATCH answers the
    way the real server does after the runner confirms the switch: the
    read-back label is stamped on the returned snapshot.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :param approval_mode: Initial ``omnigent.codex_native.approval_mode``
        label value; ``None`` simulates a session that has not switched yet
        (the picker shows its unset placeholder).
    :returns: Captured PATCH request bodies.
    """
    latest_payload: dict | None = None
    patch_bodies: list[dict] = []
    # Mutable so the PATCH handler can update it without rebinding the name.
    cur_approval_mode: list[str | None] = [approval_mode]

    def _handle(route: Route) -> None:
        nonlocal latest_payload
        request = route.request
        parsed = urlparse(request.url)
        if parsed.path != f"/v1/sessions/{session_id}":
            route.continue_()
            return

        headers = {"content-type": "application/json"}
        if request.method == "GET":
            response = fetch_with_retry(route)
            payload = response.json()
            headers = {**response.headers, **headers}
        elif request.method == "PATCH":
            request_body = json.loads(request.post_data or "{}")
            patch_bodies.append(request_body)
            payload = dict(latest_payload or {})
            if "approval_mode" in request_body:
                # The real server persists the label only after the runner
                # confirms Codex's /permissions popup applied the preset.
                cur_approval_mode[0] = request_body["approval_mode"]
        else:
            route.continue_()
            return

        labels = dict(payload.get("labels", {}))
        labels["omnigent.wrapper"] = "codex-native-ui"
        if cur_approval_mode[0] is not None:
            labels[_APPROVAL_MODE_LABEL_KEY] = cur_approval_mode[0]
        payload["labels"] = labels
        payload["harness"] = "codex"
        payload["llm_model"] = "gpt-5.5"
        latest_payload = dict(payload)
        route.fulfill(
            status=200,
            headers=headers,
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)
    return patch_bodies


def test_codex_native_approval_mode_switch_persists(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Picking an approval preset in the gear modal PATCHes the server.

    Selecting "Approve for me" sends ``{"approval_mode": "approve-for-me"}``
    to ``PATCH /v1/sessions/{id}`` — the request that drives Codex's own
    ``/permissions`` popup on the runner — and the picker then shows the
    server's stamped read-back label rather than a locally guessed value.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to a codex-native session
        that has not switched modes yet.
    :returns: None.
    """
    base_url, session_id = seeded_session
    patch_bodies = _patch_session_as_codex_native(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=15_000)
    gear.click()

    # The Approvals picker is visible for codex-native sessions even before
    # any switch; with no read-back label yet it shows its placeholder.
    picker = page.get_by_test_id("composer-config-approval-mode")
    expect(picker).to_be_visible()
    expect(picker).to_contain_text("Set in Codex")
    picker.click()

    # Located by data attribute, not accessible name: each option renders its
    # label and description together, so the name is never the bare label.
    page.locator('[role="option"][data-approval-mode="approve-for-me"]').click()

    # Save commits the draft and fires the PATCH.
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == f"/v1/sessions/{session_id}"
            and response.status == 200
        )
    ):
        page.get_by_test_id("composer-config-save").click()

    assert patch_bodies[-1] == {"approval_mode": "approve-for-me"}

    # Save closes the modal only after its awaited setter chain settles; a
    # click during that close races the dialog, so wait for it to finish.
    expect(page.get_by_test_id("composer-config-save")).not_to_be_visible()

    # The store reads the mode back from the PATCH response's stamped label,
    # so reopening the modal shows the confirmed preset, not a draft.
    gear.click()
    picker = page.get_by_test_id("composer-config-approval-mode")
    expect(picker).to_be_visible()
    expect(picker).to_contain_text("Approve for me")


def test_codex_native_approval_mode_starts_from_label(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A session with a stamped read-back label opens on that preset.

    Covers the TUI→web direction as the browser sees it: after a
    ``/permissions`` change inside Codex the server stamps
    ``omnigent.codex_native.approval_mode``, and the picker reflects it on
    load instead of the unset placeholder.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to a codex-native session
        already in ``full-access``.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_codex_native(page, session_id, approval_mode="full-access")

    page.goto(f"{base_url}/c/{session_id}")

    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=15_000)
    gear.click()

    picker = page.get_by_test_id("composer-config-approval-mode")
    expect(picker).to_be_visible()
    expect(picker).to_contain_text("Full Access")
