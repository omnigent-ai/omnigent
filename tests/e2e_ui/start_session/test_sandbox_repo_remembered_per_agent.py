"""E2E: a sandbox repo picked on New Chat stays with its agent, and a session whose managed
launch failed still names the repo it was asked to clone. The server runs the ``modal``
provider without its SDK, so every managed launch fails right after create."""

from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, Request, expect

from tests._helpers.live_server import isolated_local_server

_REPO_URL = "https://github.com/omnigent-ai/fixture-repo.git"
_REPO_NAME = "fixture-repo"
_REPO_LABEL_KEY = "omnigent.sandbox.repo"
_NO_REPO_CHIP_LABEL = "Sandbox repositories: None selected"
_AGENT_A = "claude-native-ui"
_AGENT_B = "codex-native-ui"
_CREATE_RE = re.compile(r"/v1/sessions(\?.*)?$")
_SESSION_URL_RE = re.compile(r"/c/([0-9a-f]{32})$")

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("modal") is not None,
    reason="needs the Modal SDK absent so every managed launch fails right after create",
)


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    # A desktop width keeps the workspace-bar chip labels expanded.
    size = {"width": 1280, "height": 800}
    return {**browser_context_args, "viewport": size, "record_video_size": size}


@pytest.fixture(scope="module")
def managed_sandbox_server(
    built_spa: None, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    """A server offering managed sandboxes whose launches fail (no Modal SDK)."""
    del built_spa
    tmp = tmp_path_factory.mktemp("managed_sandbox_server")
    config = tmp / "config.yaml"
    config.write_text("sandbox:\n  provider: modal\n  server_url: http://127.0.0.1\n")
    with isolated_local_server(tmp, extra_args=["--config", str(config)]) as base_url:
        yield base_url


def _agent_ids(base_url: str) -> dict[str, str]:
    with httpx.Client(base_url=base_url, timeout=20.0, trust_env=False) as client:
        agents = client.get("/v1/agents").json()["data"]
    ids = {agent["name"]: agent["id"] for agent in agents}
    missing = {_AGENT_A, _AGENT_B} - ids.keys()
    assert not missing, f"built-in agents not registered on the test server: {missing}"
    return ids


def _session_labels(base_url: str, session_id: str) -> dict[str, str]:
    with httpx.Client(base_url=base_url, timeout=20.0, trust_env=False) as client:
        response = client.get(f"/v1/sessions/{session_id}")
    response.raise_for_status()
    return response.json().get("labels") or {}


def _open_landing(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/")
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)
    # No connected hosts, so the managed sandbox is the offered default.
    expect(page.get_by_test_id("new-chat-landing-host-chip")).to_have_attribute(
        "aria-label", re.compile(r"Host: Modal Sandbox")
    )


def _pick_agent(page: Page, agent_id: str) -> None:
    trigger = page.get_by_test_id("new-chat-landing-agent-select")
    trigger.click()
    option = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    expect(option).to_be_visible(timeout=60_000)
    option.click()
    # The selected row becomes the config submenu; close it and wait for its
    # dismissal layer to unmount so the next click lands.
    if trigger.get_attribute("aria-expanded") == "true":
        page.keyboard.press("Escape")
    expect(trigger).to_have_attribute("aria-expanded", "false")
    expect(page.locator("[data-radix-popper-content-wrapper]")).to_have_count(0)


def _add_repo(page: Page, url: str, name: str) -> None:
    chip = page.get_by_test_id("new-chat-landing-repo-chip")
    expect(chip).to_have_attribute("aria-label", _NO_REPO_CHIP_LABEL)
    chip.click()
    page.get_by_test_id("new-chat-landing-repo-input").fill(url)
    page.get_by_test_id("new-chat-landing-repo-add").click()
    expect(chip).to_have_attribute("aria-label", f"Sandbox repositories: {name}")
    page.keyboard.press("Escape")
    expect(page.locator("[data-radix-popper-content-wrapper]")).to_have_count(0)


def _start_session(page: Page, message: str) -> tuple[dict[str, Any], str]:
    """Send the landing message; return the create body and the new session id."""
    page.get_by_test_id("new-chat-landing-input").fill(message)
    with page.expect_request(
        lambda request: request.method == "POST" and _CREATE_RE.search(request.url) is not None
    ) as created:
        page.get_by_test_id("new-chat-landing-submit").click()
    request: Request = created.value
    body = json.loads(request.post_data or "{}")
    expect(page).to_have_url(_SESSION_URL_RE, timeout=30_000)
    match = _SESSION_URL_RE.search(page.url)
    assert match is not None, page.url
    return body, match.group(1)


def _wait_for_launch_failure(page: Page) -> None:
    expect(page.get_by_test_id("sandbox-failed-indicator")).to_be_visible(timeout=30_000)


def test_repo_picked_for_one_agent_does_not_seed_another_agents_launch(
    request: pytest.FixtureRequest, managed_sandbox_server: str
) -> None:
    """Launching agent B after agent A used a repo must not carry that repo over."""
    base_url = managed_sandbox_server
    agents = _agent_ids(base_url)
    page: Page = request.getfixturevalue("page")

    _open_landing(page, base_url)
    _pick_agent(page, agents[_AGENT_A])
    _add_repo(page, _REPO_URL, _REPO_NAME)
    body_a, session_a = _start_session(page, "Audit the repository.")
    assert body_a["host_type"] == "managed", body_a
    assert body_a["workspaces"] == [_REPO_URL], body_a
    _wait_for_launch_failure(page)
    assert _session_labels(base_url, session_a).get(_REPO_LABEL_KEY) == _REPO_URL

    _open_landing(page, base_url)
    _pick_agent(page, agents[_AGENT_B])
    chip = page.get_by_test_id("new-chat-landing-repo-chip")
    expect(chip).to_have_attribute("aria-label", _NO_REPO_CHIP_LABEL)
    body_b, session_b = _start_session(page, "Say hello.")
    assert body_b["host_type"] == "managed", body_b
    assert body_b["workspaces"] == [], body_b
    assert _REPO_LABEL_KEY not in _session_labels(base_url, session_b)

    # Agent A's own repository is remembered and comes back when A is picked again.
    _open_landing(page, base_url)
    _pick_agent(page, agents[_AGENT_A])
    expect(chip).to_have_attribute("aria-label", f"Sandbox repositories: {_REPO_NAME}")


def test_failed_sandbox_launch_names_the_repository_it_tried_to_clone(
    request: pytest.FixtureRequest, managed_sandbox_server: str
) -> None:
    """After a managed launch with a repository fails, the composer chip names it."""
    base_url = managed_sandbox_server
    agents = _agent_ids(base_url)
    page: Page = request.getfixturevalue("page")

    _open_landing(page, base_url)
    _pick_agent(page, agents[_AGENT_A])
    _add_repo(page, _REPO_URL, _REPO_NAME)
    body, session_id = _start_session(page, "Audit the repository.")
    assert body["workspaces"] == [_REPO_URL], body
    _wait_for_launch_failure(page)
    assert _session_labels(base_url, session_id).get(_REPO_LABEL_KEY) == _REPO_URL

    chip = page.get_by_test_id("composer-workspace-dir")
    expect(chip).to_have_text(_REPO_NAME)
    expect(chip).to_have_attribute("title", f"Sandbox repository: {_REPO_URL}")
