"""
Browser e2e guard: a managed Kubernetes sandbox whose workspace-prep init
container crash-loops must fail the launch fast, and the failure banner must
carry the init container's log tail (the git clone error).

Journey: an operator runs the server with ``sandbox.provider: kubernetes``
against a cluster whose sandbox namespace cannot reach the clone host. On the
new-chat landing the user picks the New Sandbox host, adds a repository URL and
sends a message; the server submits a Job whose ``workspace-prep`` init
container keeps failing its ``git clone`` and is restarted by the kubelet (Pod
``Pending``, init container ``CrashLoopBackOff``), so it never comes up.

No cluster is reachable from the test environment, so a stub ``kubernetes``
package on the server subprocess's PYTHONPATH replays the apiserver's view of
that state (see ``tests/e2e/_k8s_crashloop_stub_sdk``). The server process,
its config parsing, the managed-session create, the launcher's start wait, the
failure-message builder and the SPA are real.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import Response, async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop
from tests._helpers.live_server import find_free_port
from tests.e2e._k8s_crashloop_server import AGENT_NAME, spawn_server, terminate, wait_for_health
from tests.e2e._k8s_crashloop_stub_sdk import CLONE_ERROR_LINE

_REPO_URL = "https://github.com/omnigent-ai/omnigent"
_PROMPT = "Investigate this repository"

# Pod-ready budget: generous enough that a fail-fast well under it is
# unambiguous, small enough that the buggy poll-to-the-deadline path keeps the
# run (and its recording) short. 90s in a default deployment.
_POD_READY_TIMEOUT_S = 25

# The stub parks workspace-prep in CrashLoopBackOff ~3s after the Job is
# submitted; a fail-fast start wait surfaces the banner a few polls later,
# while the buggy path cannot fail before the 25s deadline.
_FAILFAST_MAX_S = 15.0


def _agent_id(base_url: str) -> str:
    """Resolve the test agent's id from the server's agent list."""
    agents = httpx.get(f"{base_url}/v1/agents", timeout=10.0).json()["data"]
    return next(agent["id"] for agent in agents if agent["name"] == AGENT_NAME)


async def _drive_launch_to_failure(
    base_url: str, agent_id: str, screenshot: Path, outcome: dict[str, Any]
) -> None:
    """Drive new chat → New Sandbox → repo URL → send; watch the launch fail.

    Records into *outcome*: ``session_id`` (from the unmodified create
    response), ``elapsed_s`` (submit click → failure banner visible) and
    ``error`` (the expanded failure text shown to the user).
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        # Keep any recording at viewport size so the expanded error stays legible.
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            record_video_size={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        async def _capture_create(response: Response) -> None:
            if response.request.method == "POST" and response.url.endswith("/v1/sessions"):
                outcome["create_status"] = response.status
                if response.ok:
                    outcome["session_id"] = (await response.json())["id"]

        page.on("response", _capture_create)
        try:
            await page.goto(f"{base_url}/")
            prompt = page.get_by_test_id("new-chat-landing-input")
            await prompt.wait_for(state="visible", timeout=60_000)

            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id(f"new-chat-landing-agent-{agent_id}").click()

            await page.get_by_test_id("new-chat-landing-host-chip").click()
            await page.get_by_test_id("new-chat-landing-sandbox-option").click()

            controls = page.get_by_test_id("new-chat-landing-workspace-controls")
            repository = controls.get_by_test_id("new-chat-landing-repo-chip")
            await repository.click()
            repo_input = page.get_by_test_id("new-chat-landing-repo-input")
            await repo_input.fill(_REPO_URL)
            # Enter commits the URL like the Add button, which re-renders with
            # the popover's repo-list state and flakes pointer clicks.
            await repo_input.press("Enter")
            await expect(repository).to_have_attribute(
                "aria-label", "Sandbox repositories: omnigent", timeout=15_000
            )
            await page.keyboard.press("Escape")

            await prompt.fill(_PROMPT)
            started = time.monotonic()
            await page.get_by_test_id("new-chat-landing-submit").click()
            await page.wait_for_url("**/c/*", timeout=30_000)

            failed = page.get_by_test_id("sandbox-failed-indicator")
            await failed.wait_for(state="visible", timeout=(_POD_READY_TIMEOUT_S + 90) * 1_000)
            outcome["elapsed_s"] = time.monotonic() - started

            await failed.get_by_test_id("error-pill").click()
            content = failed.get_by_test_id("error-message-content")
            await content.wait_for(state="visible", timeout=15_000)
            outcome["error"] = await content.inner_text()
            await page.screenshot(path=str(screenshot))
            if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
                # Hold the expanded error on screen so the recording ends on it.
                await page.wait_for_timeout(4_000)
        finally:
            await context.close()
            await browser.close()


def test_init_crashloop_launch_fails_fast_with_log_tail(tmp_path: Path) -> None:
    """The launch must fail fast and its banner must carry the clone error.

    A crash-looping init container leaves the Pod ``Pending`` under
    ``restartPolicy: OnFailure``; the start wait must report it within seconds
    and attach the init container's log instead of polling out the pod-ready
    budget with Pod events only.
    """
    port = find_free_port()
    proc, log_path = spawn_server(tmp_path, port, _POD_READY_TIMEOUT_S)
    outcome: dict[str, Any] = {}
    try:
        base_url = f"http://127.0.0.1:{port}"
        wait_for_health(proc, base_url, log_path)
        agent_id = _agent_id(base_url)
        run_in_fresh_loop(
            _drive_launch_to_failure(base_url, agent_id, tmp_path / "launch-failed.png", outcome)
        )
    finally:
        terminate(proc)
        print(
            "k8s-init-crashloop outcome:",
            json.dumps(
                {
                    **outcome,
                    "pod_ready_timeout_s": _POD_READY_TIMEOUT_S,
                    "server_log": str(log_path),
                    "screenshot": str(tmp_path / "launch-failed.png"),
                }
            ),
        )

    elapsed = outcome["elapsed_s"]
    error = outcome["error"]
    problems: list[str] = []
    if elapsed >= _FAILFAST_MAX_S:
        problems.append(
            f"launch failure took {elapsed:.1f}s — the crash-looping workspace-prep "
            f"init container was not fail-fast detected and the start wait polled to "
            f"its {_POD_READY_TIMEOUT_S}s pod-ready deadline (90s in a default "
            f"deployment)"
        )
    if CLONE_ERROR_LINE not in error:
        problems.append(
            "the failure banner dropped the workspace-prep log tail — the user never "
            "sees the git clone error the init container printed; error shown: "
            f"{error[:800]}"
        )
    if problems:
        pytest.fail("\n".join(problems))
