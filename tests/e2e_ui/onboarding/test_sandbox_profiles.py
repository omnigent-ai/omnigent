"""Own-environment browser proof with real profile API/SQLite and no cluster.

Only provisioning requests are intercepted. Discovery, authentication and
revocation use the installed provider and isolated Omnigent server.
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

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.live_server import isolated_local_server


@pytest.fixture
def sandbox_profile_server(
    built_spa: None, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[str]:
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
        tmp_path / "server",
        env={"OMNIGENT_FEATURES": "sandbox_profiles" if getattr(request, "param", True) else ""},
        extra_args=["--config", str(config)],
        cwd=tmp_path,
    ) as server:
        yield server


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
    template = catalog["profiles"][1]
    catalog["profiles"].extend(
        {
            **template,
            "id": f"build-{i:02}",
            "name": f"Specialized build {i:02}",
            "description": "Approved tools and resources for a specialized development workflow.",
        }
        for i in range(1, 36)
    )
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
            "OMNIGENT_FEATURES": "sandbox_profiles",
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
    page.goto(f"{base}/settings/sandboxes")
    expect(page.get_by_role("heading", name="General", exact=True, level=1)).to_be_visible()
    expect(page.get_by_test_id("settings-nav-sandboxes")).to_have_count(0)
    page.goto(base)
    chooser = page.get_by_role("button", name="Sandbox profile: Choose profile", exact=True)
    expect(chooser).to_be_visible()
    page.get_by_test_id("new-chat-landing-input").fill(
        "Build the project in the approved sandbox."
    )
    expect(page.get_by_role("button", name="Start session", exact=True)).to_be_disabled()
    chooser.click()
    expect(page.get_by_role("option", name="Default", exact=False)).to_have_count(0)
    page.screenshot(
        path=evidence / "provider-03-alice-choices.png", full_page=True, animations="disabled"
    )
    expect(page.get_by_role("option")).to_have_count(20)
    expect(page.get_by_text("Showing first 20 matches. Refine your search.")).to_be_visible()
    page.get_by_role("combobox", name="Search sandbox profiles").fill("Specialized build 35")
    page.get_by_role("option", name="Specialized build 35", exact=False).click()
    expect(page.get_by_role("button", name="Start session", exact=True)).to_be_enabled()

    with httpx.Client(base_url=base, headers=alice, trust_env=False) as client:
        listing = client.get("/v1/sandbox-profiles").json()
        assert listing["managed_providers"] == ["profile_demo"]
        assert "can_manage" not in listing
        selected = next(p for p in listing["profiles"] if p["id"] == "build-35")
        assert "config" not in selected
        assert "users" not in selected and "user_id" not in selected
        assert client.put(
            "/v1/sandbox-profiles/demo-large",
            json={"provider": "profile_demo", "name": "Tampered", "config": {}},
        ).status_code in (404, 405)
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

    store = SqlAlchemyConversationStore(f"sqlite:///{catalog_file.parent / 'server/chat.db'}")
    store.accept_sandbox_profile(
        source_id, {**selected, "user_id": "alice@example.test", "workspace_id": 0}
    )
    page.goto(f"{base}/c/{source_id}")
    page.get_by_test_id("header-conversation-actions").click()
    page.get_by_test_id("header-fork-conversation").click()
    expect(page.get_by_test_id("fork-session-dialog")).to_be_visible()
    page.get_by_test_id("fork-session-host-select").click()
    page.get_by_test_id("fork-session-sandbox-option").click()
    source_choice = page.get_by_role(
        "button", name="Sandbox profile: Specialized build 35", exact=True
    )
    expect(source_choice).to_be_visible()
    source_choice.click()
    expect(page.get_by_role("option")).to_have_count(20)
    page.get_by_role("combobox", name="Search sandbox profiles").fill("Large development")
    page.get_by_role("option", name="Large development sandbox", exact=False).click()
    page.get_by_role(
        "button", name="Sandbox profile: Large development sandbox", exact=True
    ).click()
    page.get_by_role("combobox", name="Search sandbox profiles").fill("Specialized build 35")
    page.get_by_role("option", name="Specialized build 35", exact=False).click()
    page.screenshot(path=evidence / "provider-06-fork.png", full_page=True, animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(
        page.get_by_role("button", name="Sandbox profile: Specialized build 35", exact=True)
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
        page.get_by_role("option", name="Small development sandbox", exact=False)
    ).to_be_visible()
    expect(
        page.get_by_role("option", name="Large development sandbox", exact=False)
    ).to_have_count(0)
    expect(page.get_by_role("option", name="Default", exact=False)).to_have_count(0)
    page.screenshot(
        path=evidence / "provider-04-bob-mobile.png", full_page=True, animations="disabled"
    )
    page.get_by_role("option", name="Small development sandbox", exact=False).click()
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
    expect(page.get_by_role("option", name="Default", exact=False)).to_have_count(0)
    expect(
        page.get_by_role("option", name="Small development sandbox", exact=False)
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


@pytest.mark.parametrize("sandbox_profile_server", [False], indirect=True)
@pytest.mark.parametrize("older_server", [False, True])
def test_default_launch_without_profile_feature(
    page: Page, sandbox_profile_server: str, older_server: bool, tmp_path: Path
) -> None:
    base = sandbox_profile_server
    requests: list[str] = []
    launches: list[dict[str, Any]] = []

    def legacy_info(route: Route) -> None:
        response = route.fetch()
        data = response.json()
        assert data["features"]["sandbox_profiles"] is False
        if older_server:
            data.pop("features")
        route.fulfill(response=response, json=data)

    def reject_profile_query(route: Route) -> None:
        requests.append(route.request.url)
        route.fulfill(status=404, json={"detail": "Not Found"})

    def capture_launch(route: Route) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        launches.append(route.request.post_data_json)
        route.fulfill(status=422, json={"detail": "Provisioning is disconnected in this test."})

    page.route("**/v1/info", legacy_info)
    page.route("**/v1/sandbox-profiles*", reject_profile_query)
    page.route("**/v1/sessions", capture_launch)
    page.goto(f"{base}/settings/sandboxes")
    expect(page.get_by_role("heading", name="General", exact=True, level=1)).to_be_visible()
    expect(page.get_by_test_id("settings-nav-sandboxes")).to_have_count(0)
    page.goto(base)
    page.get_by_test_id("new-chat-landing-input").fill("Start the ordinary sandbox")
    expect(page.get_by_role("button", name="Sandbox profile:")).to_have_count(0)
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url == f"{base}/v1/sessions"
        )
    ):
        page.get_by_role("button", name="Start session", exact=True).click()
    assert len(launches) == 1
    assert launches[0]["host_type"] == "managed"
    assert "sandbox_profile_id" not in launches[0]
    assert "sandbox_profile_revision" not in launches[0]
    assert requests == []
    evidence = Path(os.environ.get("OMNIGENT_SANDBOX_PROFILE_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=evidence / f"default-launch-older-{older_server}.png", full_page=True)
