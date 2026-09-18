"""E2E: harness readiness on a credential-less host.

Journey from the bug report: self-host ``omnigent host`` from an environment
whose image ships the harness CLIs but has **no** credentials configured, then
open the new-session picker against that host. The daemon's readiness map
(``configured_harnesses`` in its hello frame) drives the picker's warnings.

Unlike the stubbed availability tests in this directory, these tests register a
REAL ``omnigent host`` daemon (isolated ``HOME``, no provider keys or CLI
logins) against the live e2e server, so the daemon's actual readiness map — not
a hand-written wire body — is what the picker renders.

* ``test_sdk_harness_readiness_reflects_missing_credentials`` is the
  regression test for the headline facet: the daemon used to report the
  in-process SDK harnesses (``claude-sdk`` / ``openai-agents``) as ready no
  matter what, so the picker showed no warning and the failure only surfaced
  when the first turn died with an auth error. It fails whenever readiness for
  SDK harnesses claims ``true`` on a host with no resolvable credential.
* ``test_cli_harness_readiness_warns_without_credentials`` guards the already
  fixed facets of the same report on the real daemon path: ``pi`` reports
  ``needs-auth`` (not available) when its binary is present but no credential
  is configured, the picker warns for it, and ``claude-native`` is a readiness
  entry distinct from ``claude-sdk``.

The async-in-a-fresh-thread shape is inherited from
``start_session/test_start_session.py``: once a pytest-playwright sync test has
run in the session, pytest-asyncio can't start a loop on the main thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from tests.e2e_ui.conftest import _register_agent_yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]

_HOST_ONLINE_TIMEOUT_S = 180.0
_FIRST_TURN_ERROR_TIMEOUT_MS = 240_000


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* to completion in a dedicated thread with its own event loop."""
    captured: dict[str, Exception] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except Exception as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


def _fetch_host_row(base_url: str, host_name: str) -> dict[str, Any] | None:
    hosts = httpx.get(f"{base_url}/v1/hosts", timeout=10.0).json().get("hosts", [])
    return next(
        (h for h in hosts if h.get("name") == host_name and h.get("status") == "online"),
        None,
    )


@pytest.fixture(scope="module")
def credentialless_host(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """A real ``omnigent host`` daemon with harness CLIs on PATH but no credentials.

    Spawns the daemon with an isolated ``HOME`` (so no ``config.yaml``
    providers, no ``~/.claude`` / ``~/.codex`` / ``~/.pi`` logins) and an
    environment stripped of every ambient API key, then waits for the server's
    REST surface to report it online. Yields the host's ``/v1/hosts`` row plus
    a ``workspace`` directory sessions on it can use.
    """
    tmp = tmp_path_factory.mktemp("credentialless_host_home")
    host_home = tmp / "home"
    host_home.mkdir()
    workspace = tmp / "workspace"
    workspace.mkdir()
    host_name = f"credless-{uuid.uuid4().hex[:8]}"

    # Minimal allow-list env: keeps the harness CLIs resolvable but leaves no
    # path to a credential (no *_API_KEY, no ambient config).
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(host_home),
        "PYTHONPATH": os.pathsep.join(
            [
                str(_REPO_ROOT),
                str(_REPO_ROOT / "sdks" / "python-client"),
                str(_REPO_ROOT / "sdks" / "ui"),
            ]
        ),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "OMNIGENT_HOST_NAME": host_name,
        "OMNIGENT_HOST_ID": uuid.uuid4().hex,
    }
    log_path = tmp / "host.log"
    with log_path.open("w") as log_handle:
        argv = [sys.executable, "-m", "omnigent", "host", "--server", live_server]
        proc = subprocess.Popen(
            [*argv, "--non-interactive"],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
        row: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            row = _fetch_host_row(live_server, host_name)
            if row is not None:
                break
            if proc.poll() is not None:
                raise RuntimeError(
                    f"omnigent host exited early ({proc.returncode}):\n"
                    f"{log_path.read_text()[-2000:]}"
                )
            time.sleep(1.0)
        if row is None:
            raise RuntimeError(
                f"credential-less host never came online:\n{log_path.read_text()[-2000:]}"
            )
        yield {**row, "workspace": str(workspace)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _register_harness_agent(base_url: str, name: str, harness: str, model: str) -> str:
    agent_id = _register_agent_yaml(
        base_url,
        (
            "spec_version: 1\n"
            f"name: {name}\n"
            "prompt: You are a test agent.\n"
            "executor:\n"
            "  config:\n"
            f"    model: {model}\n"
            f"    harness: {harness}\n"
        ),
    )
    assert agent_id is not None, f"agent {name} failed to register"
    return agent_id


async def _select_agent(page: Any, agent_id: str) -> None:
    """Pick *agent_id* in the landing picker's agent menu, then dismiss it."""
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    await picker.click()
    await expect(page.get_by_role("menu").first).to_be_visible()
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    if await row.count() == 0:
        # Session-registered agents fold into the "Other..." flyout.
        custom = page.get_by_test_id("new-chat-landing-custom-agents")
        if await custom.count() > 0:
            await custom.hover()
            with contextlib.suppress(Exception):
                await row.wait_for(state="visible", timeout=5_000)
    if await row.count() == 0:
        more = page.get_by_test_id("new-chat-landing-harness-more")
        if await more.count() > 0:
            await more.click()
    await row.click()
    await page.keyboard.press("Escape")
    if await picker.get_attribute("aria-expanded") == "true":
        await page.keyboard.press("Escape")


async def _seed_recent_workspace(page: Any, host_id: str, workspace: str) -> None:
    await page.add_init_script(
        "window.localStorage.setItem("
        '"omnigent:recent-workspaces", '
        f"JSON.stringify({json.dumps({host_id: [workspace]})}))"
    )


def test_sdk_harness_readiness_reflects_missing_credentials(
    live_server: str,
    credentialless_host: dict[str, Any],
) -> None:
    """A credential-less host must not present claude-sdk as ready with no warning.

    Reproduces the reported journey end to end: the picker offers the Claude
    SDK agent on the credential-less host without any readiness warning, the
    session launches, and the first turn dies with an auth error the user was
    never warned about. Passes once the host's readiness map (and therefore
    the picker) reflects that no credential is resolvable for the SDK harness.
    """
    _run_in_fresh_loop(_drive_sdk_readiness(live_server, credentialless_host))


async def _drive_sdk_readiness(base_url: str, host: dict[str, Any]) -> None:
    agent_id = _register_harness_agent(
        base_url, f"credless-claude-sdk-{uuid.uuid4().hex[:6]}", "claude-sdk", "claude-sonnet-4-5"
    )
    warned_before_launch = False
    first_turn_error: str | None = None
    session_id: str | None = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _seed_recent_workspace(page, host["host_id"], host["workspace"])
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await _select_agent(page, agent_id)

            # The under-composer readiness notice for the selected agent on the
            # selected host. Today it never appears for an SDK harness; after
            # the fix it must.
            warning = page.get_by_test_id("new-chat-landing-harness-warning")
            try:
                await expect(warning).to_be_visible(timeout=15_000)
                warned_before_launch = True
            except AssertionError:
                warned_before_launch = False

            # Launch anyway (the readiness signal warns, it does not block) and
            # watch the first turn. Today it dies with an auth error the picker
            # never hinted at; post-fix the launch may still fail the same way,
            # so this is journey evidence, not the regression assertion.
            await page.get_by_test_id("new-chat-landing-input").fill(
                "hello from the readiness reproduction"
            )
            await page.get_by_test_id("new-chat-landing-submit").click()
            await page.wait_for_url(re.compile(r"/c/[0-9a-f]+"), timeout=60_000)
            match = re.search(r"/c/([0-9a-f]+)", page.url)
            session_id = match.group(1) if match else None
            error_pill = page.locator('[data-testid="error-pill"]').first
            try:
                await error_pill.wait_for(state="visible", timeout=_FIRST_TURN_ERROR_TIMEOUT_MS)
                first_turn_error = (await error_pill.inner_text()).strip()
            except Exception:
                first_turn_error = None
            if session_id is not None:
                items = httpx.get(
                    f"{base_url}/v1/sessions/{session_id}/items", timeout=10.0
                ).json()
                for item in items.get("data", []):
                    if item.get("type") == "error":
                        first_turn_error = item.get("message") or first_turn_error
        finally:
            await page.close()
            await browser.close()
            if session_id is not None:
                with contextlib.suppress(Exception):
                    httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)

    row = _fetch_host_row(base_url, host["name"])
    assert row is not None, "credential-less host dropped offline mid-test"
    availability = (row.get("configured_harnesses") or {}).get("claude-sdk")
    assert availability is not True, (
        "host with no credentials reported claude-sdk as ready "
        f"(configured_harnesses['claude-sdk'] == {availability!r}); "
        f"the launched session's first turn then failed with: {first_turn_error!r}"
    )
    assert warned_before_launch, (
        "picker showed no readiness warning for a claude-sdk agent on a "
        f"credential-less host; the first turn then failed with: {first_turn_error!r}"
    )


def test_cli_harness_readiness_warns_without_credentials(
    live_server: str,
    credentialless_host: dict[str, Any],
) -> None:
    """Installed-but-credential-less pi reports needs-auth and the picker warns.

    Guards the already-landed facets of the same report: pi's readiness is no
    longer binary presence (its ``pi`` CLI is on PATH here, yet the host
    reports ``needs-auth``), the picker surfaces that as a warning, and
    ``claude-native`` / ``claude-sdk`` are independent readiness entries.
    """
    if shutil.which("pi") is None:
        pytest.skip("pi CLI not installed on this machine")
    _run_in_fresh_loop(_drive_pi_readiness(live_server, credentialless_host))


async def _drive_pi_readiness(base_url: str, host: dict[str, Any]) -> None:
    agent_id = _register_harness_agent(
        base_url, f"credless-pi-{uuid.uuid4().hex[:6]}", "pi", "claude-sonnet-4-5"
    )

    row = _fetch_host_row(base_url, host["name"])
    assert row is not None, "credential-less host dropped offline mid-test"
    harnesses = row.get("configured_harnesses") or {}
    assert harnesses.get("pi") == "needs-auth", (
        "pi CLI is installed with no credential, so the host must report "
        f"needs-auth, got {harnesses.get('pi')!r}"
    )
    assert "claude-native" in harnesses and "claude-sdk" in harnesses, (
        "claude-native and claude-sdk must be distinct readiness entries, got "
        f"{sorted(harnesses)!r}"
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _seed_recent_workspace(page, host["host_id"], host["workspace"])
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await _select_agent(page, agent_id)
            warning = page.get_by_test_id("new-chat-landing-harness-warning")
            await expect(warning).to_be_visible(timeout=30_000)
            await expect(warning).to_contain_text(host["name"])
        finally:
            await page.close()
            await browser.close()
