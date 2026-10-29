"""E2E: the user's own idle session must not trigger the directory-conflict warning.

A real ``omnigent host`` daemon runs against the test server so the landing can
browse a directory and launch a runner there. The first session is created
through the landing composer and left idle with its runner still attached;
opening a second new session's directory selector at the same directory must
show no "other agent is working in this directory" banner.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.async_api import Locator, Page, async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop
from tests._helpers.host import isolated_host
from tests.e2e_ui.conftest import configure_mock_llm
from tests.e2e_ui.start_session.helpers import (
    commit_landing_workspace_picker,
    dismiss_landing_agent_menu,
    open_landing_workspace_picker,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
# Two SPA runner-health poll cycles (POLL_INTERVAL_MS in web/src/hooks/useRunnerHealth.ts),
# so one delayed poll cannot pass the observation vacuously.
_CONFLICT_OBSERVATION_MS = 20_000
# Control window: with no session on the picked host nothing is registered for
# health polling, so a banner here would not depend on the poll cycle.
_NO_CONFLICT_BASELINE_MS = 3_000
_AGENT_ROW = re.compile(r"^hello[ _]world$", re.I)


async def _becomes_visible(locator: Locator, timeout_ms: int) -> bool:
    try:
        await expect(locator).to_be_visible(timeout=timeout_ms)
    except AssertionError:
        return False
    return True


async def _select_hello_world(page: Page) -> None:
    trigger = page.get_by_test_id("new-chat-landing-agent-select")
    await trigger.click()
    await expect(trigger).to_have_attribute("aria-expanded", "true")
    row = page.get_by_role("menuitem", name=_AGENT_ROW)
    # The server's hello_world agent is listed under "Other..." unless configured top-level.
    if not await _becomes_visible(row, 2_000):
        await page.get_by_test_id("new-chat-landing-custom-agents").click()
    await expect(row).to_be_visible(timeout=15_000)
    await row.click()
    await dismiss_landing_agent_menu(page)


async def _select_host(page: Page, host_id: str) -> None:
    await page.get_by_test_id("new-chat-landing-host-chip").click()
    await page.get_by_test_id(f"new-chat-landing-host-{host_id}").click()
    await expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_be_enabled(
        timeout=15_000
    )
    await expect(page.locator("[data-radix-popper-content-wrapper]")).to_have_count(0)


async def _browse_to_project(page: Page, project: Path) -> None:
    await open_landing_workspace_picker(page)
    path_input = page.get_by_test_id("workspace-picker-path-input")
    # Typing before the host home resolves lets the late resolve overwrite the path.
    await expect(path_input).not_to_have_value("", timeout=15_000)
    if await path_input.input_value() != str(project):
        await path_input.fill(str(project))
        await path_input.press("Enter")
    await expect(page.get_by_test_id("workspace-picker-entry-README.md")).to_be_visible(
        timeout=15_000
    )


async def _wait_session_idle(
    client: httpx.AsyncClient, session_id: str, *, timeout_s: float = 90.0
) -> None:
    deadline = time.monotonic() + timeout_s
    last = "<never fetched>"
    while time.monotonic() < deadline:
        resp = await client.get(f"/v1/sessions/{session_id}", timeout=10.0)
        if resp.status_code == 200:
            last = resp.json().get("status", "<missing>")
            if last == "idle":
                return
        await asyncio.sleep(0.5)
    raise AssertionError(f"session {session_id} never went idle (last status: {last})")


async def _drive(base_url: str, mock_llm_url: str, tmp_path: Path) -> None:
    marker = uuid.uuid4().hex[:8]
    prompt = f"idle-occupancy hello {marker}"
    reply = f"idle-occupancy-reply-{marker}"
    configure_mock_llm(
        mock_llm_url, [{"text": reply}], key=f"idle-occupancy-{marker}", match=prompt
    )

    host_root = tmp_path / "host"
    project = host_root / "home" / "project"
    project.mkdir(parents=True)
    (project / "README.md").write_text("hello\n")
    host_env = {
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_llm_url}/v1",
        "PYTHONPATH": str(_REPO_ROOT),
    }

    session_id: str | None = None
    sync_client = httpx.Client(base_url=base_url, timeout=10.0)
    try:
        with isolated_host(host_root, base_url, env=host_env) as host:
            host.wait_online(sync_client, timeout=60, poll_interval=0.25)
            async with async_playwright() as pw, httpx.AsyncClient(base_url=base_url) as client:
                browser = await pw.chromium.launch()
                context = await browser.new_context(viewport={"width": 1280, "height": 800})
                page = await context.new_page()
                try:
                    await page.goto(base_url)
                    await expect(page.get_by_test_id("new-chat-landing")).to_be_visible(
                        timeout=15_000
                    )
                    await _select_hello_world(page)
                    await _select_host(page, host.host_id)
                    await _browse_to_project(page, project)
                    conflict = page.get_by_test_id("workspace-picker-conflict")
                    # Control: nothing works in the directory yet, so no banner may show.
                    assert not await _becomes_visible(conflict, _NO_CONFLICT_BASELINE_MS), (
                        "conflict banner shown for a directory with no session"
                    )
                    await commit_landing_workspace_picker(page)

                    await page.get_by_test_id("new-chat-landing-input").fill(prompt)
                    await page.get_by_test_id("new-chat-landing-submit").click()
                    await expect(page.get_by_text(reply)).to_be_visible(timeout=180_000)
                    await page.wait_for_url(re.compile(r"/c/(?!temp)"), timeout=30_000)
                    session_id = page.url.rstrip("/").split("/c/")[-1].split("?")[0]
                    await _wait_session_idle(client, session_id)

                    listed = (
                        await client.get("/v1/sessions?limit=200&visibility=all", timeout=10.0)
                    ).json()["data"]
                    occupants = [
                        s["id"]
                        for s in listed
                        if s.get("host_id") == host.host_id and s.get("workspace") == str(project)
                    ]
                    assert occupants == [session_id], occupants
                    health = (
                        await client.get(
                            "/health", params={"session_ids": session_id}, timeout=10.0
                        )
                    ).json()
                    assert health["sessions"][session_id]["runner_online"] is True, health

                    await page.get_by_test_id("new-chat-button").click()
                    await expect(page.get_by_test_id("new-chat-landing")).to_be_visible(
                        timeout=15_000
                    )
                    await _select_host(page, host.host_id)
                    await _browse_to_project(page, project)

                    if await _becomes_visible(conflict, _CONFLICT_OBSERVATION_MS):
                        text = " ".join((await conflict.inner_text()).split())
                        pytest.fail(
                            f"Phantom conflict warning {text!r}: the only session in "
                            f"{project} is the user's own idle session "
                            "(runner online, no turn running)."
                        )
                finally:
                    with contextlib.suppress(Exception):
                        await context.close()
                    with contextlib.suppress(Exception):
                        await browser.close()
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                sync_client.delete(f"/v1/sessions/{session_id}", timeout=30.0)
        sync_client.close()


def test_sole_idle_session_shows_no_directory_conflict_warning(
    live_server: str, mock_llm_server_url: str, tmp_path: Path
) -> None:
    run_in_fresh_loop(_drive(live_server, mock_llm_server_url, tmp_path))
