"""E2E: landing sandbox repos are remembered per agent; a failed launch still names its repo."""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.async_api import Page, async_playwright, expect

from tests.e2e_ui.start_session.helpers import select_landing_agent
from tests.e2e_ui.start_session.test_start_session import _run_in_fresh_loop

_REPO_ROOT = Path(__file__).resolve().parents[3]

_HEALTH_TIMEOUT_S = 90.0
# The managed launch fails at provisioning (no Modal SDK), so the failure band
# normally lands within seconds; the budget covers a loaded CI box.
_LAUNCH_SETTLE_TIMEOUT_MS = 60_000

_REPO_URL = "https://github.com/omnigent-ai/fixture-repo.git"
_REPO_NAME = "fixture-repo"
# Server label recording the repositories a managed session was created with
# (``MANAGED_REPO_LABEL_KEY``), collapsed to this bare key on the wire.
_SANDBOX_REPO_LABEL_KEY = "omnigent.sandbox.repo"
_NO_REPO_CHIP_LABEL = "Sandbox repositories: None selected"

# Two built-in agents that the landing picker lists inline for a sandbox host.
_AGENT_A = "claude-native-ui"
_AGENT_B = "codex-native-ui"

# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY that must not
# intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)

# Ambient runner/host identity would make the spawned server treat this
# process as one of its runners (see dev/recording-lanes.md).
_AMBIENT_ENV_PREFIXES = ("OMNIGENT_RUNNER", "OMNIGENT_HOST", "OMNIGENT_REPRO")
_AMBIENT_ENV_KEYS = {"RUNNER_SERVER_URL", "OMNIGENT_REMOTE_AUTH_TOKEN"}


@dataclass
class _ManagedRig:
    """A dedicated server advertising a managed sandbox provider."""

    base_url: str
    agent_ids: dict[str, str]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_healthy(proc: subprocess.Popen[bytes], base_url: str, log_path: Path) -> None:
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(
        f"managed-sandbox server did not become healthy within {_HEALTH_TIMEOUT_S:.0f}s.\n"
        f"Server log:\n{log_path.read_text()[-3000:]}"
    )


# With the Modal provider configured but its SDK absent, /v1/info advertises managed
# sandboxes, the landing offers the sandbox host and repository chip, and every managed
# launch fails right after the create - the state the failed-launch journey needs.
@pytest.fixture(scope="module")
def managed_rig(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[_ManagedRig]:
    """Spawn a server offering the Modal sandbox provider; no runner is involved."""
    if request.config.getoption("--ui-base-url"):
        pytest.skip("needs a spawned server with a sandbox provider configured")

    work = tmp_path_factory.mktemp("managed_repo_rig")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    config_path = work / "server.yaml"
    config_path.write_text(f"sandbox:\n  provider: modal\n  server_url: {base_url}\n")

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_AMBIENT_ENV_PREFIXES) and key not in _AMBIENT_ENV_KEYS
    }
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_REPO_ROOT), env.get("PYTHONPATH", "")]))
    env["NO_PROXY"] = ",".join(filter(None, [env.get("NO_PROXY", ""), "127.0.0.1,localhost"]))

    log_path = work / "server.log"
    with log_path.open("w") as log_handle:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{work / 'test.db'}",
                "--artifact-location",
                str(work / "artifacts"),
                "--config",
                str(config_path),
            ],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )
        try:
            _wait_healthy(proc, base_url, log_path)
            info = _client.get(f"{base_url}/v1/info").json()
            assert info.get("managed_sandboxes_enabled") is True, info
            agents = _client.get(f"{base_url}/v1/agents").json()["data"]
            agent_ids = {agent["name"]: agent["id"] for agent in agents}
            assert {_AGENT_A, _AGENT_B} <= agent_ids.keys(), sorted(agent_ids)
            yield _ManagedRig(base_url=base_url, agent_ids=agent_ids)
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def _session_labels(rig: _ManagedRig, session_id: str) -> dict[str, str]:
    response = _client.get(f"{rig.base_url}/v1/sessions/{session_id}", timeout=10)
    response.raise_for_status()
    return response.json().get("labels") or {}


async def _open_new_chat(page: Page, rig: _ManagedRig) -> None:
    await page.goto(f"{rig.base_url}/")
    await page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)
    # No connected hosts, so the managed sandbox is the offered default.
    await expect(page.get_by_test_id("new-chat-landing-host-chip")).to_have_attribute(
        "aria-label", re.compile("Modal Sandbox")
    )


async def _add_sandbox_repo(page: Page) -> None:
    chip = page.get_by_test_id("new-chat-landing-repo-chip")
    await expect(chip).to_have_attribute("aria-label", _NO_REPO_CHIP_LABEL)
    await chip.click()
    await page.get_by_test_id("new-chat-landing-repo-input").fill(_REPO_URL)
    await page.get_by_test_id("new-chat-landing-repo-add").click()
    await expect(chip).to_have_attribute("aria-label", f"Sandbox repositories: {_REPO_NAME}")
    await page.keyboard.press("Escape")


async def _start_session(page: Page, message: str, *, not_session: str | None = None) -> str:
    await page.get_by_test_id("new-chat-landing-input").fill(message)
    await page.get_by_test_id("new-chat-landing-submit").click()
    exclude = f"(?!{re.escape(not_session)})" if not_session else ""
    await page.wait_for_url(re.compile(rf"/c/{exclude}[0-9a-f]{{32}}$"), timeout=30_000)
    return page.url.rsplit("/", 1)[1]


async def _wait_for_launch_failure(page: Page) -> None:
    await expect(page.get_by_test_id("sandbox-failed-indicator")).to_be_visible(
        timeout=_LAUNCH_SETTLE_TIMEOUT_MS
    )


def test_remembered_sandbox_repo_does_not_ride_into_another_agents_launch(
    managed_rig: _ManagedRig,
) -> None:
    """A repo picked for agent A is not pre-selected for, nor sent with, agent B."""
    _run_in_fresh_loop(_drive_remembered_repo(managed_rig))


async def _drive_remembered_repo(rig: _ManagedRig) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        try:
            await _open_new_chat(page, rig)
            await select_landing_agent(page, rig.agent_ids[_AGENT_A])
            await _add_sandbox_repo(page)
            first = await _start_session(page, "Audit the repository.")
            await _wait_for_launch_failure(page)
            assert _session_labels(rig, first).get(_SANDBOX_REPO_LABEL_KEY) == _REPO_URL

            await _open_new_chat(page, rig)
            await select_landing_agent(page, rig.agent_ids[_AGENT_B])
            chip_label = await page.get_by_test_id("new-chat-landing-repo-chip").get_attribute(
                "aria-label"
            )
            second = await _start_session(page, "Say hello.", not_session=first)
            await _wait_for_launch_failure(page)
            second_labels = _session_labels(rig, second)

            assert chip_label == _NO_REPO_CHIP_LABEL, (
                f"agent B's untouched repository chip read {chip_label!r}"
            )
            assert _SANDBOX_REPO_LABEL_KEY not in second_labels, (
                f"agent A's repository rode into agent B's launch: {second_labels}"
            )
        finally:
            await context.close()
            await browser.close()


def test_failed_sandbox_launch_names_the_repository_it_was_sent(
    managed_rig: _ManagedRig,
) -> None:
    """A session whose sandbox launch failed still shows which repo was sent."""
    _run_in_fresh_loop(_drive_failed_launch(managed_rig))


async def _drive_failed_launch(rig: _ManagedRig) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        try:
            await _open_new_chat(page, rig)
            await select_landing_agent(page, rig.agent_ids[_AGENT_A])
            await _add_sandbox_repo(page)
            session_id = await _start_session(page, "Audit the repository.")
            await _wait_for_launch_failure(page)
            assert _session_labels(rig, session_id).get(_SANDBOX_REPO_LABEL_KEY) == _REPO_URL

            workspace_chip = page.get_by_test_id("composer-workspace-dir")
            await expect(workspace_chip).to_be_visible()
            await expect(page.get_by_role("main")).to_contain_text(_REPO_NAME, timeout=5_000)
        finally:
            await context.close()
            await browser.close()
