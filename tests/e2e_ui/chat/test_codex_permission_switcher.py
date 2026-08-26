"""E2E coverage for live Codex permission switching from the composer gear."""

from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, expect


def test_codex_permission_switcher_updates_real_harness(
    page: Page,
    native_codex_session: tuple[str, str],
) -> None:
    """Switch a real Codex app-server thread to read-only and back."""
    base_url, session_id = native_codex_session
    page.goto(f"{base_url}/c/{session_id}")

    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=30_000)
    gear.click()
    modal = page.get_by_test_id("composer-config-modal")
    expect(modal).to_be_visible()
    expect(modal.get_by_text("Permissions", exact=True)).to_be_visible()
    # Capability is known from the wrapper label, so the Effort control must
    # render with the rest of the modal rather than waiting for model/list.
    expect(page.get_by_test_id("composer-config-effort")).to_be_visible(timeout=1_000)
    picker = page.get_by_test_id("composer-config-codex-approval-mode")
    expect(picker).to_be_visible()
    expect(picker).to_have_attribute("aria-label", "Permissions")
    picker.click()
    expect(page.get_by_role("option")).to_have_count(3)
    for label in ("Default", "Full access", "Read only"):
        expect(page.get_by_role("option", name=label, exact=True)).to_be_visible()
    expect(
        page.get_by_role("option", name="Bypass approvals & sandbox", exact=True)
    ).to_have_count(0)
    page.locator('[role="option"][data-approval-mode="read-only"]').click()

    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == f"/v1/sessions/{session_id}"
            and response.request.post_data_json == {"codex_approval_mode": "read-only"}
        ),
        timeout=30_000,
    ) as response_info:
        page.get_by_role("button", name="Save").click()

    assert response_info.value.status == 200, response_info.value.text()
    assert response_info.value.request.post_data_json == {"codex_approval_mode": "read-only"}
    session = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
    assert session["terminal_launch_args"] == [
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "on-request",
    ]

    page.reload()
    expect(gear).to_be_visible(timeout=30_000)
    gear.click()
    expect(picker).to_contain_text("Read only")
    picker.click()
    page.locator('[role="option"][data-approval-mode="default"]').click()
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == f"/v1/sessions/{session_id}"
            and response.request.post_data_json == {"codex_approval_mode": "default"}
            and response.status == 200
        ),
        timeout=30_000,
    ):
        page.get_by_role("button", name="Save").click()

    session = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
    assert session["terminal_launch_args"] == []
