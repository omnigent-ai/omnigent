"""Own-environment browser proof with real profile API/SQLite and no cluster.

Only the session launch request is intercepted: profile creation, editing,
disabling, reload, and discovery use the actual isolated Omnigent server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


@pytest.fixture
def provider_profile_server(built_spa: None, tmp_path: Path) -> Iterator[tuple[str, Path]]:
    repo = Path(__file__).resolve().parents[3]
    example = repo / "examples/sandbox-profile-provider"
    plugin_dir = tmp_path / "plugin"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--no-deps",
            "--target",
            str(plugin_dir),
            str(example),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    catalog = json.loads((example / "catalog.json").read_text())
    for profile in catalog["profiles"]:
        profile["users"] = [f"{user}@example.test" for user in profile["users"]]
        profile["workspaces"] = [0]
    catalog_file = tmp_path / "catalog.json"
    catalog_file.write_text(json.dumps(catalog))
    config = tmp_path / "config.yaml"
    config.write_text(
        "admins: [alice@example.test]\n"
        "sandbox:\n"
        "  provider: profile_demo\n"
        "  server_url: http://sandbox-browser-test.invalid\n"
        "  profile_demo:\n"
        f"    catalog_file: {catalog_file}\n"
    )
    with isolated_local_server(
        tmp_path / "server",
        extra_args=["--config", str(config)],
        cwd=tmp_path,
        env={
            "OMNIGENT_LOCAL_SINGLE_USER": "0",
            "PYTHONPATH": os.pathsep.join([str(repo), str(plugin_dir)]),
        },
    ) as server:
        yield server, catalog_file


def test_provider_owned_profiles(
    page: Page, provider_profile_server: tuple[str, Path], tmp_path: Path
) -> None:
    base, catalog_file = provider_profile_server
    evidence = Path(os.environ.get("OMNIGENT_SANDBOX_PROFILE_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    alice = {"X-Forwarded-Email": "alice@example.test"}
    bob = {"X-Forwarded-Email": "bob@example.test"}
    page.set_extra_http_headers(alice)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{base}/settings/general")
    page.get_by_test_id("settings-nav-sandboxes").click()
    expect(page.get_by_text("Managed by provider", exact=True)).to_have_count(2)
    expect(page.get_by_role("button", name="New profile", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Edit Large development sandbox")).to_have_count(0)
    page.screenshot(
        path=evidence / "provider-01-readonly.png", full_page=True, animations="disabled"
    )
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base}/settings/general")
    page.get_by_role("button", name="Back to settings menu", exact=True).click()
    page.get_by_test_id("settings-nav-sandboxes").click()
    expect(page.get_by_text("Managed by provider", exact=True)).to_have_count(2)
    page.screenshot(
        path=evidence / "provider-02-settings-mobile.png", full_page=True, animations="disabled"
    )
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(base)
    chooser = page.get_by_role("button", name="Sandbox profile: Choose profile", exact=True)
    expect(chooser).to_be_visible()
    page.get_by_test_id("new-chat-landing-input").fill(
        "Build the project in the approved sandbox."
    )
    expect(page.get_by_role("button", name="Start session", exact=True)).to_be_disabled()
    chooser.click()
    expect(page.get_by_role("menuitem", name="Default", exact=False)).to_have_count(0)
    page.screenshot(
        path=evidence / "provider-03-alice-choices.png", full_page=True, animations="disabled"
    )
    page.get_by_role("menuitem", name="Large development sandbox", exact=False).click()
    expect(page.get_by_role("button", name="Start session", exact=True)).to_be_enabled()

    with httpx.Client(base_url=base, headers=alice, trust_env=False) as client:
        listing = client.get("/v1/sandbox-profiles").json()
        assert listing["managed_providers"] == ["profile_demo"]
        assert listing["can_manage"] is False
        selected = next(p for p in listing["profiles"] if p["id"] == "demo-large")
        assert selected["config"] == {}
        assert "users" not in selected and "user_id" not in selected
        assert (
            client.put(
                "/v1/sandbox-profiles/demo-large",
                json={"provider": "profile_demo", "name": "Tampered", "config": {}},
            ).status_code
            == 403
        )
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
        lambda r: r.request.method == "POST" and r.url == f"{base}/v1/sessions"
    ):
        page.get_by_role("button", name="Start session", exact=True).click()
    assert launches[0]["sandbox_profile_id"] == selected["id"]
    assert launches[0]["sandbox_profile_revision"] == selected["revision"]
    expect(page.get_by_test_id("new-chat-landing-input")).to_have_value(
        "Build the project in the approved sandbox."
    )
    with httpx.Client(base_url=base, trust_env=False) as client:
        for profile_fields, identity, expected_status in [
            ({}, alice, 422),
            (
                {
                    "sandbox_profile_id": selected["id"],
                    "sandbox_profile_revision": selected["revision"],
                },
                bob,
                403,
            ),
        ]:
            response = client.post(
                "/v1/sessions",
                headers=identity,
                json={
                    "agent_id": launches[0]["agent_id"],
                    "host_type": "managed",
                    "sandbox_provider": "profile_demo",
                    **profile_fields,
                },
            )
            assert response.status_code == expected_status, response.text
            assert "profile" in response.text.lower()
        source = client.post(
            "/v1/sessions",
            headers=alice,
            json={
                "agent_id": launches[0]["agent_id"],
                "host_type": "external",
                "workspace": "/workspace/demo",
            },
        )
        source.raise_for_status()
        source_id = source.json()["id"]

    page.goto(f"{base}/c/{source_id}")
    page.get_by_test_id("header-conversation-actions").click()
    page.get_by_test_id("header-fork-conversation").click()
    expect(page.get_by_test_id("fork-session-dialog")).to_be_visible()
    page.get_by_test_id("fork-session-host-select").click()
    page.get_by_test_id("fork-session-sandbox-option").click()
    page.get_by_role("button", name="Sandbox profile: Choose profile", exact=True).click()
    page.get_by_role("menuitem", name="Large development sandbox", exact=False).click()
    page.screenshot(path=evidence / "provider-06-fork.png", full_page=True, animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(
        page.get_by_role("button", name="Sandbox profile: Large development sandbox", exact=True)
    ).to_be_visible()
    page.screenshot(
        path=evidence / "provider-07-fork-mobile.png", full_page=True, animations="disabled"
    )
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.route(f"**/v1/sessions/{source_id}/fork", intercept_launch)
    with page.expect_response(lambda response: response.url.endswith(f"/{source_id}/fork")):
        page.get_by_test_id("fork-session-submit").click()
    assert launches[1]["sandbox_profile_id"] == selected["id"]
    assert launches[1]["sandbox_profile_revision"] == selected["revision"]
    expect(page.get_by_test_id("fork-session-dialog")).to_be_visible()

    page.set_extra_http_headers(bob)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base)
    expect(page.get_by_role("button", name="Sandbox profile:", exact=False)).to_be_visible()
    close_sidebar = page.get_by_role("button", name="Close sidebar", exact=True)
    if close_sidebar.is_visible():
        close_sidebar.click(position={"x": 380, "y": 20})
        expect(close_sidebar).to_be_hidden()
    page.get_by_role("button", name="Sandbox profile:", exact=False).click()
    expect(
        page.get_by_role("menuitem", name="Small development sandbox", exact=False)
    ).to_be_visible()
    expect(
        page.get_by_role("menuitem", name="Large development sandbox", exact=False)
    ).to_have_count(0)
    expect(page.get_by_role("menuitem", name="Default", exact=False)).to_have_count(0)
    page.screenshot(
        path=evidence / "provider-04-bob-mobile.png", full_page=True, animations="disabled"
    )
    page.get_by_role("menuitem", name="Small development sandbox", exact=False).click()
    with httpx.Client(base_url=base, headers=bob, trust_env=False) as client:
        assert [p["id"] for p in client.get("/v1/sandbox-profiles").json()["profiles"]] == [
            "demo-small"
        ]

    catalog = json.loads(catalog_file.read_text())
    for profile in catalog["profiles"]:
        profile["users"] = []
    catalog_file.write_text(json.dumps(catalog))
    page.reload()
    page.get_by_role("button", name="Sandbox profile:", exact=False).click()
    expect(page.get_by_role("menuitem", name="Default", exact=False)).to_have_count(0)
    expect(
        page.get_by_role("menuitem", name="Small development sandbox", exact=False)
    ).to_have_count(0)
    page.keyboard.press("Escape")
    page.get_by_test_id("new-chat-landing-input").fill("This must not launch after revocation.")
    expect(page.get_by_role("button", name="Start session", exact=True)).to_be_disabled()
    page.screenshot(
        path=evidence / "provider-05-revoked.png", full_page=True, animations="disabled"
    )
    catalog_file.write_text("{invalid-private-catalog")
    page.reload()
    expect(
        page.get_by_role("button", name="Sandbox profile: Profiles unavailable", exact=True)
    ).to_be_visible()
    expect(page.get_by_role("button", name="Start session", exact=True)).to_be_disabled()
    assert "invalid-private-catalog" not in page.locator("body").inner_text()
