"""E2E regression: an installed, capable Codex must not be flagged "outdated".

A user on a remote host has a working ``codex`` CLI, yet the web
picker flags Codex as "outdated" and the launch gate refuses it, so they
cannot proceed with Codex. The classification comes from the host daemon:
``_codex_auth_unavailable_reason`` reports ``"version-too-low"`` whenever
``harness_cli_installed("openai")`` fails while the binary is on ``PATH``.
That fires for any codex below the date-derived ``_CODEX_MIN_VERSION =
"0.137.0"`` floor — including versions at or above the **documented
capability floor 0.129.0** (the native policy hook requirement recorded in
``omnigent/onboarding/harness_install.py`` and enforced in
``omnigent/harnesses/codex_native/app_server.py``) — and equally for a
working codex whose ``--version`` output the probe cannot parse or that
times out.

This test drives the real product path end to end, with no ``/v1/hosts``
stubbing: a real ``omnigent host`` daemon is spawned against the live test
server with a working ``codex`` on ``PATH`` reporting 0.133.0 (policy-hook
compatible, below the date floor, standing in for the Arca host's codex
install). The daemon's own readiness probe classifies it, the hello frame
carries the map through ``GET /v1/hosts``, and the SPA's new-chat landing
renders the result. Only ``/v1/agents`` is stubbed (established picker-test
pattern) so a Codex agent is selectable; agents are orthogonal to the bug.

Expected: a host whose installed codex satisfies every
capability the native harness needs is selectable — the composer must not
claim the CLI is outdated, and the host readiness map must not read
``"version-too-low"``. On the unfixed build both assertions fail.

The async-in-a-fresh-thread shape is inherited from
``chat/test_codex_auth_availability.py`` for the reason documented there.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import Route, async_playwright, expect

from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable

_REPO_ROOT = Path(__file__).resolve().parents[3]

# At or above the documented capability floor (the native policy hook needs
# codex >= 0.129.0), below the date-derived setup floor (0.137.0). A codex at
# this version launches and enforces policy hooks fine — flagging it
# "outdated" and refusing it is the false positive this bug reports.
_STUB_CODEX_VERSION = "0.133.0"

# The daemon computes its readiness map at startup by probing every harness
# CLI (each probe capped at 10s), so first appearance of the codex entry can
# lag the host coming online on a busy CI box.
_HOST_READINESS_TIMEOUT_S = 150.0
_POLL_INTERVAL_S = 0.5


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* in a dedicated thread with its own event loop.

    The e2e_ui suite runs pytest-playwright **sync** tests in the same
    session; once one has run, pytest-asyncio can't start a loop on the main
    thread. Any exception (including assertion failures) is re-raised on the
    calling thread so the test fails normally.
    """
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


def _write_codex_stub(bin_dir: Path) -> None:
    """Write a working ``codex`` CLI that reports a capability-compatible version.

    Stands in for the remote (Arca) host's codex install: present on
    ``PATH``, executable, and answering ``--version`` promptly — the same
    observable surface the real CLI gives the readiness probe.
    """
    codex = bin_dir / "codex"
    codex.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "--version" ]]; then\n'
        f'  echo "codex-cli {_STUB_CODEX_VERSION}"\n'
        "  exit 0\n"
        "fi\n"
        'echo "codex stub: ok"\n'
        "exit 0\n"
    )
    codex.chmod(0o755)


def _await_codex_readiness(base_url: str, daemon_log: Path) -> dict[str, Any]:
    """Poll ``GET /v1/hosts`` until an online host reports codex readiness.

    :returns: The host entry (with ``configured_harnesses``) once its map
        contains a ``codex-native`` verdict.
    """
    deadline = time.monotonic() + _HOST_READINESS_TIMEOUT_S
    last: Any = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/v1/hosts", timeout=10.0)
        except httpx.HTTPError:
            resp = None
        if resp is not None and resp.status_code == 200:
            hosts = resp.json().get("hosts", [])
            last = hosts
            for host in hosts:
                readiness = host.get("configured_harnesses") or {}
                if host.get("status") == "online" and "codex-native" in readiness:
                    return host
        time.sleep(_POLL_INTERVAL_S)
    log_tail = daemon_log.read_text()[-2000:] if daemon_log.exists() else "<no log>"
    raise AssertionError(
        "Host daemon never reported codex readiness within "
        f"{_HOST_READINESS_TIMEOUT_S}s; last /v1/hosts: {last!r}; "
        f"daemon log tail:\n{log_tail}"
    )


@pytest.fixture
def codex_stub_host(live_server: str, tmp_path: Path) -> Iterator[dict[str, Any]]:
    """Spawn a real host daemon whose ``PATH`` leads with the codex stub.

    The daemon runs with an isolated ``HOME`` (own identity/registry, no
    ambient credentials) and without inherited runner/host/mock-LLM env, so
    its readiness map reflects exactly the staged machine state. Yields the
    host's ``/v1/hosts`` entry plus a real workspace path on that host.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_codex_stub(bin_dir)
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["HOME"] = str(host_home)
    # Never inherit this process's runner/host identity (a leaked
    # OMNIGENT_RUNNER_* set makes the child take the zygote path) nor the
    # suite's mock-LLM provider env (it would skew codex auth detection).
    for var in [v for v in env if v.startswith(("OMNIGENT_RUNNER", "OMNIGENT_HOST"))]:
        env.pop(var, None)
    for var in (
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "RUNNER_SERVER_URL",
        "OMNIGENT_REMOTE_AUTH_TOKEN",
    ):
        env.pop(var, None)
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    apply_runner_env(env)

    daemon_log = tmp_path / "host-daemon.log"
    with open(daemon_log, "w") as log_fh:
        daemon = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=env,
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    try:
        host = _await_codex_readiness(live_server, daemon_log)
        yield {"host": host, "workspace": str(workspace)}
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=10)


def _codex_agents_body() -> str:
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_codex_native_ui",
                    "name": "codex-native-ui",
                    "display_name": "Codex",
                    "description": "OpenAI's coding agent",
                    "harness": "codex-native",
                    "skills": [],
                }
            ]
        }
    )


async def _register_agent_routes(page) -> None:
    """Stub ``/v1/agents`` (+ the agent scan) so a Codex agent is selectable.

    ``/v1/hosts`` is deliberately NOT stubbed — the host readiness map under
    test flows from the real daemon through the real endpoint.
    """

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_codex_agents_body())

    async def handle_agent_scan(route: Route) -> None:
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        )

    await page.route("**/v1/agents", handle_agents)
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)


def test_installed_codex_not_flagged_outdated(
    codex_stub_host: dict[str, Any],
    live_server: str,
) -> None:
    """A host with a working, capability-compatible codex must not read "outdated".

    Journey: connect a host that has codex installed and working
    → open the new-chat screen and pick the Codex agent on that host →
    the composer must not flag Codex's CLI as outdated, and the host's
    readiness map must not classify codex as ``version-too-low``.
    """
    _run_in_fresh_loop(_drive_codex_picker(live_server, codex_stub_host))


async def _drive_codex_picker(base_url: str, stub_host: dict[str, Any]) -> None:
    host = stub_host["host"]
    host_id = host["host_id"]
    host_name = host.get("name", host_id)
    codex_state = (host.get("configured_harnesses") or {}).get("codex-native")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_agent_routes(page)
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {host_id!r}: [{stub_host["workspace"]!r}] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            warning = page.get_by_test_id("new-chat-landing-harness-warning")
            if codex_state is not True:
                # Some warning is legitimate here (e.g. needs-auth on a host
                # that never ran `codex login`) — wait for it so the outdated
                # copy, if the bug is present, is definitely rendered before
                # the assertion below reads it.
                await expect(warning).to_be_visible(timeout=30_000)
            # Dwell so the settled picker state is readable in recordings.
            await page.wait_for_timeout(2_000)

            # The bug: an installed codex satisfying the native harness's own
            # documented capability floor (>= 0.129.0) is presented to the
            # user as an outdated CLI needing an upgrade.
            await expect(warning).not_to_contain_text("has an outdated CLI", timeout=5_000)

            # And the wire-level classification that drives every codex
            # surface (picker badge, composer warning, launch gate) must not
            # call it version-too-low.
            assert codex_state != "version-too-low", (
                f"Host {host_name!r} classified its installed codex "
                f"{_STUB_CODEX_VERSION} (>= capability floor 0.129.0) as "
                f"'version-too-low' — rendered as 'outdated' in the picker "
                f"and refused by the launch gate. Full map entry: "
                f"{host.get('configured_harnesses', {}).get('codex-native')!r}"
            )
        finally:
            # Close the context before the browser so a recording, when one
            # was requested, is finalized even on a failing run.
            await page.context.close()
            await browser.close()
