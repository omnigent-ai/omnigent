"""Clone a repository from a URL, then start a session in the clone.

Both entry points run against a real ``omnigent host`` daemon, so the browser
request travels through the server's host tunnel to a real ``git clone``. The
host's git config rewrites ``https://git.example.test/`` to local bare
repositories, which keeps the clone offline while the user still types an
ordinary https URL. Only the model is mocked.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.compat import runner_executable
from tests._helpers.host import isolated_host
from tests.e2e_ui.conftest import configure_mock_llm

_HOST_ONLINE_TIMEOUT_S = 180.0
_FAKE_FORGE = "https://git.example.test/"


@dataclass(frozen=True)
class CloneHost:
    base_url: str
    host_id: str
    name: str
    home: Path
    agent_id: str


def _make_remote(forge: Path, slug: str) -> None:
    """Create a bare repository at ``forge/<slug>.git`` with one commit."""
    source = forge / f"{slug}-source"
    source.mkdir(parents=True)
    (source / "README.md").write_text(f"# {slug.rsplit('/', 1)[1]}\n")
    git = ["git", "-c", "user.name=E2E", "-c", "user.email=e2e@example.test"]
    subprocess.run([*git, "init", "-q", "-b", "main", str(source)], check=True)
    subprocess.run([*git, "-C", str(source), "add", "."], check=True)
    subprocess.run([*git, "-C", str(source), "commit", "-q", "-m", "init"], check=True)
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(source), str(forge / f"{slug}.git")], check=True
    )


def _hello_world_agent_id(base_url: str) -> str:
    agents = httpx.get(f"{base_url}/v1/agents", timeout=10.0, trust_env=False).json()["data"]
    return next(agent["id"] for agent in agents if agent["name"] == "hello_world")


@pytest.fixture(scope="module")
def clone_host(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[CloneHost]:
    """A real host whose git resolves the fake forge to local bare repositories."""
    root = tmp_path_factory.mktemp("clone_repository_host")
    forge = root / "forge"
    for slug in ("acme/demo-service", "acme/billing-api"):
        _make_remote(forge, slug)
    base_env = {
        "PATH": os.environ["PATH"],
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
    }
    host_env = {
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{forge.as_uri()}/.insteadOf",
        "GIT_CONFIG_VALUE_0": _FAKE_FORGE,
    }
    name = f"laptop-{uuid.uuid4().hex[:6]}"
    with (
        httpx.Client(base_url=live_server, trust_env=False, timeout=30.0) as client,
        isolated_host(
            root / "host",
            live_server,
            env=host_env,
            base_env=base_env,
            name=name,
            command=[
                runner_executable(),
                "-m",
                "omnigent",
                "host",
                "--server",
                live_server,
                "--non-interactive",
            ],
        ) as host,
    ):
        host.wait_online(client, timeout=_HOST_ONLINE_TIMEOUT_S, poll_interval=1)
        yield CloneHost(
            live_server, host.host_id, name, host.home, _hello_world_agent_id(live_server)
        )


def _seed_composer(page: Page, host: CloneHost) -> None:
    """Start from this host and the mock-backed agent, as a returning user would."""
    page.add_init_script(
        f"window.localStorage.setItem('omnigent:last-host-choice', {json.dumps(host.host_id)});"
        f"window.localStorage.setItem('omnigent:last-agent-id', {json.dumps(host.agent_id)});"
    )


def _pause(page: Page, milliseconds: int) -> None:
    """Hold a recorded journey long enough to read each step; no-op otherwise."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(milliseconds)


def _send_first_message(page: Page, mock_url: str, text: str) -> str:
    reply = f"Ready to work in {text.rsplit(' ', 1)[1]}."
    configure_mock_llm(mock_url, [{"text": reply}, {"text": reply}], match=text)
    page.get_by_test_id("new-chat-landing-input").press_sequentially(text, delay=30)
    _pause(page, 1000)
    page.get_by_test_id("new-chat-landing-submit").click()
    page.wait_for_url(re.compile(r"/c/[0-9a-f]+"), timeout=60_000)
    match = re.search(r"/c/([0-9a-f]+)", page.url)
    assert match is not None, page.url
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]')
    expect(assistant.filter(has_text=reply)).to_be_visible(timeout=120_000)
    return match.group(1)


def _assert_session_in(base_url: str, session_id: str, host_id: str, workspace: Path) -> None:
    snapshot = httpx.get(
        f"{base_url}/v1/sessions/{session_id}", timeout=10.0, trust_env=False
    ).json()
    assert snapshot.get("host_id") == host_id, snapshot
    assert snapshot.get("workspace") == str(workspace), snapshot


def _delete_sessions(base_url: str, session_ids: list[str]) -> None:
    for session_id in session_ids:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0, trust_env=False)


def _delete_projects_named(base_url: str, name: str) -> None:
    with (
        contextlib.suppress(httpx.HTTPError),
        httpx.Client(base_url=base_url, trust_env=False, timeout=10.0) as client,
    ):
        for project in client.get("/v1/projects").json()["data"]:
            if project["name"] == name:
                client.delete(f"/v1/projects/{project['id']}")


def test_clone_repository_from_composer_workspace_menu(
    request: pytest.FixtureRequest, clone_host: CloneHost, mock_llm_server_url: str
) -> None:
    """Clone from the working-directory menu, then start a session in the clone."""
    destination = clone_host.home / "demo-service"
    sessions: list[str] = []
    page: Page = request.getfixturevalue("page")
    _seed_composer(page, clone_host)
    request.addfinalizer(lambda: _delete_sessions(clone_host.base_url, sessions))
    page.goto(f"{clone_host.base_url}/")
    chip = page.get_by_test_id("new-chat-landing-workspace-chip")
    expect(chip).to_be_visible(timeout=30_000)
    _pause(page, 1500)

    chip.click()
    clone_item = page.get_by_test_id("new-chat-landing-workspace-clone-repository")
    expect(clone_item).to_be_visible()
    _pause(page, 1500)
    clone_item.click()
    dialog = page.get_by_test_id("clone-repository-dialog")
    expect(dialog).to_be_visible()
    _pause(page, 1500)
    page.get_by_test_id("clone-repository-url").press_sequentially(
        f"{_FAKE_FORGE}acme/demo-service.git", delay=40
    )
    expect(page.get_by_test_id("clone-repository-destination")).to_contain_text("~/demo-service")
    _pause(page, 2500)
    page.get_by_test_id("clone-repository-confirm").click()
    expect(dialog).to_be_hidden(timeout=60_000)
    expect(chip).to_have_text("demo-service")
    assert (destination / "README.md").read_text() == "# demo-service\n"
    _pause(page, 2500)

    sessions.append(
        _send_first_message(page, mock_llm_server_url, "Summarize the README in demo-service")
    )
    _assert_session_in(clone_host.base_url, sessions[0], clone_host.host_id, destination)
    _pause(page, 4000)


def test_new_project_clones_repository_as_its_default_workspace(
    request: pytest.FixtureRequest, clone_host: CloneHost, mock_llm_server_url: str
) -> None:
    """A project created with a clone starts its sessions in that clone."""
    destination = clone_host.home / "billing-api"
    sessions: list[str] = []
    page: Page = request.getfixturevalue("page")
    _seed_composer(page, clone_host)
    request.addfinalizer(lambda: _delete_sessions(clone_host.base_url, sessions))
    request.addfinalizer(lambda: _delete_projects_named(clone_host.base_url, "billing-api"))
    page.goto(f"{clone_host.base_url}/")
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)
    _pause(page, 1500)

    page.get_by_test_id("new-project").click()
    page.get_by_test_id("new-project-clone-toggle").click()
    expect(page.get_by_test_id("new-project-clone-section")).to_be_visible()
    _pause(page, 1500)
    page.get_by_test_id("new-project-clone-host").click()
    page.get_by_role("option", name=clone_host.name, exact=True).click()
    expect(page.get_by_test_id("new-project-clone-host")).to_contain_text(clone_host.name)
    # The closing select returns focus to its trigger; type only after it settles.
    expect(page.get_by_role("listbox")).to_be_hidden()
    page.get_by_test_id("clone-repository-url").click()
    page.get_by_test_id("clone-repository-url").press_sequentially(
        f"{_FAKE_FORGE}acme/billing-api.git", delay=40
    )
    expect(page.get_by_test_id("new-project-name")).to_have_attribute(
        "placeholder", "Project name (default: billing-api)"
    )
    _pause(page, 2500)
    page.get_by_test_id("new-project-confirm").click()
    expect(page.get_by_test_id("new-project-clone-section")).to_be_hidden(timeout=60_000)
    assert (destination / "README.md").read_text() == "# billing-api\n"

    header = page.get_by_role("button", name="billing-api", exact=True)
    expect(header).to_be_visible()
    _pause(page, 2000)
    header.hover()
    _pause(page, 1000)
    page.get_by_role("link", name="New session in billing-api", exact=True).click()
    chip = page.get_by_test_id("new-chat-landing-workspace-chip")
    expect(chip).to_have_text("billing-api", timeout=30_000)
    _pause(page, 2500)

    sessions.append(
        _send_first_message(page, mock_llm_server_url, "List the open tasks in billing-api")
    )
    _assert_session_in(clone_host.base_url, sessions[0], clone_host.host_id, destination)
    _pause(page, 4000)
