"""Own-environment browser proof with real profile API/SQLite and no cluster.

Only the session launch request is intercepted: profile creation, editing,
disabling, reload, and discovery use the actual isolated Omnigent server.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests._helpers.live_server import isolated_local_server


@pytest.fixture
def sandbox_profile_server(built_spa: None, tmp_path: Path) -> Iterator[str]:
    config = tmp_path / "config.yaml"
    config.write_text(
        "admins: [local]\n"
        "sandbox:\n"
        "  provider: kubernetes\n"
        "  server_url: http://sandbox-browser-test.invalid\n"
        "  kubernetes:\n"
        "    namespace: sandbox-browser-test\n"
    )
    with isolated_local_server(
        tmp_path / "server", extra_args=["--config", str(config)], cwd=tmp_path
    ) as server:
        yield server


def test_manage_and_select_sandbox_profile(
    page: Page, sandbox_profile_server: str, tmp_path: Path
) -> None:
    base = sandbox_profile_server
    evidence = Path(os.environ.get("OMNIGENT_SANDBOX_PROFILE_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{base}/settings/general")
    page.get_by_test_id("settings-nav-sandboxes").click()
    page.get_by_role("button", name="New profile", exact=True).click()
    page.get_by_label("Name", exact=True).fill("Large workspace")
    page.get_by_label("Profile ID", exact=True).fill("large-workspace")
    page.get_by_label("Description", exact=True).fill("Extra memory for builds and test suites")
    configuration = {
        "resources": {
            "requests": {"cpu": "1", "memory": "2Gi"},
            "limits": {"cpu": "2", "memory": "4Gi"},
        }
    }
    editor = page.get_by_label("Sandbox configuration", exact=True)
    editor.fill("[]")
    expect(page.get_by_role("alert")).to_contain_text("must be a JSON object")
    expect(page.get_by_role("button", name="Save profile")).to_be_disabled()
    editor.fill(json.dumps(configuration, indent=2))
    expect(page.get_by_role("button", name="Save profile")).to_be_enabled()
    page.screenshot(path=evidence / "01-profile-editor.png", full_page=True, animations="disabled")
    page.get_by_role("button", name="Save profile").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.get_by_role("heading", name="Large workspace", exact=True)).to_be_visible()

    # Reload verifies that the saved list comes from the API, not local form state.
    page.reload()
    page.get_by_role("button", name="Edit Large workspace").click()
    assert json.loads(editor.input_value()) == configuration
    expect(page.get_by_label("Profile ID", exact=True)).to_be_disabled()
    page.get_by_label("Description", exact=True).fill("Builds and tests with 4 GiB of memory")
    page.get_by_role("button", name="Save profile").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    page.screenshot(path=evidence / "02-saved-profiles.png", full_page=True, animations="disabled")

    with httpx.Client(trust_env=False) as client:
        response = client.get(f"{base}/v1/sandbox-profiles")
        response.raise_for_status()
        saved = response.json()["profiles"][0]
    assert saved["config"] == configuration
    assert saved["description"] == "Builds and tests with 4 GiB of memory"

    page.goto(base)
    chooser = page.get_by_role("button", name="Sandbox profile: Default", exact=True)
    expect(chooser).to_be_visible()
    chooser.click()
    page.get_by_text("Large workspace", exact=True).click()
    expect(page.get_by_role("button", name="Sandbox profile: Large workspace")).to_be_visible()
    page.get_by_test_id("new-chat-landing-input").fill("Build the project and run the test suite.")
    page.screenshot(
        path=evidence / "03-selected-profile.png", full_page=True, animations="disabled"
    )
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="Close sidebar", exact=True).click(
        position={"x": 380, "y": 20}
    )
    page.get_by_role("button", name="Sandbox profile: Large workspace").click()
    expect(
        page.get_by_role("menuitem", name="Default Use the server configuration")
    ).to_be_visible()
    page.keyboard.press("Escape")
    expect(
        page.get_by_role("menuitem", name="Default Use the server configuration")
    ).to_be_hidden()
    page.screenshot(
        path=evidence / "04-selected-profile-mobile.png", full_page=True, animations="disabled"
    )
    page.set_viewport_size({"width": 1440, "height": 1000})

    launches: list[dict[str, Any]] = []

    def intercept_launch(route: Route) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        launches.append(route.request.post_data_json)
        route.fulfill(
            status=422,
            content_type="application/json",
            body=json.dumps({"detail": "Provisioning is disconnected in this browser test."}),
        )

    page.route("**/v1/sessions", intercept_launch)
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url == f"{base}/v1/sessions"
        )
    ) as launch_response:
        page.get_by_role("button", name="Start session", exact=True).click()
    assert launch_response.value.status == 422
    expect(page.get_by_test_id("new-chat-landing-input")).to_have_value(
        "Build the project and run the test suite."
    )
    assert len(launches) == 1
    assert launches[0]["sandbox_profile_id"] == saved["id"]
    assert launches[0]["sandbox_profile_revision"] == saved["revision"]
    (evidence / "launch-request.json").write_text(json.dumps(launches[0], indent=2))

    page.goto(f"{base}/settings/sandboxes")
    page.get_by_role("button", name="Disable Large workspace").click()
    expect(page.get_by_text("Disabled", exact=True)).to_be_visible()
    with httpx.Client(trust_env=False) as client:
        profiles = client.get(f"{base}/v1/sandbox-profiles").json()["profiles"]
    assert profiles[0]["enabled"] is False

    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base}/settings/general")
    page.get_by_role("button", name="Back to settings menu", exact=True).click()
    page.get_by_test_id("settings-nav-sandboxes").click()
    page.get_by_role("button", name="Edit Large workspace").click()
    expect(page.get_by_label("Name", exact=True)).to_have_value("Large workspace")
    expect(page.get_by_role("switch", name="Enabled")).not_to_be_checked()
    page.screenshot(
        path=evidence / "05-profile-editor-mobile.png", full_page=True, animations="disabled"
    )
    page.get_by_role("switch", name="Enabled").click()
    page.get_by_role("button", name="Save profile").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.get_by_text("Disabled", exact=True)).to_have_count(0)
    with httpx.Client(trust_env=False) as client:
        profiles = client.get(f"{base}/v1/sandbox-profiles").json()["profiles"]
    assert profiles[0]["enabled"] is True
