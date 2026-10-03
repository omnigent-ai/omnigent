"""E2E: a working Codex CLI above its capability floor must not read "outdated".

Journey from the bug report: a user's remote host has a codex CLI
that runs fine from the shell, yet the new-session composer flags Codex as
outdated on that host and steers the session onto a different harness, forcing
the user to fall back.

Like ``chat/test_credentialless_host_harness_readiness.py``, this registers a
REAL ``omnigent host`` daemon (isolated ``HOME``, allow-list env) against the
live e2e server, so the daemon's actual readiness map — not a hand-written
wire body — is what the picker renders. The daemon's ``PATH`` leads with a
working stub ``codex`` reporting version 0.133.0: at or above the native
harness's real capability floor (the 0.129.0 policy hook) yet below the
0.137.0 floor setup once derived from a release date — the range a capable
user-installed codex can legitimately occupy.

The journey lands on the built-in "Codex" agent (``codex-native-ui``) and the
stub host the way a returning user does. It fails while the daemon classifies
that working CLI ``version-too-low``: the readiness map reports it, and the
composer auto-swaps Codex for another harness with an "outdated" notice. It
passes once the daemon reads the CLI as ready or ``needs-auth`` (this host
holds no codex login or provider credential), so Codex stays selectable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
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
from playwright.async_api import async_playwright

_REPO_ROOT = Path(__file__).resolve().parents[3]

_HOST_ONLINE_TIMEOUT_S = 180.0

# Above the 0.129.0 policy-hook capability floor, inside the range a
# date-derived 0.137.0 floor once falsely rejected: fully usable by the
# native harness, so readiness must not call it outdated.
_CODEX_STUB_VERSION = "0.133.0"


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


def _codex_agent_id(base_url: str) -> str:
    """Return the built-in Codex agent's id from the catalog, as the picker sees it."""
    rows = httpx.get(f"{base_url}/v1/agents?limit=100", timeout=10.0).json()["data"]
    codex = next((r for r in rows if r.get("harness") == "codex-native"), None)
    assert codex is not None, f"no built-in codex-native agent in catalog: {rows!r}"
    return codex["id"]


@pytest.fixture(scope="module")
def codex_stub_host(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """A real ``omnigent host`` daemon whose PATH leads with a working codex stub.

    The stub answers ``--version`` with :data:`_CODEX_STUB_VERSION` and exits 0
    for everything else, standing in for a user-installed codex the daemon's
    version probe must judge. The isolated ``HOME`` and allow-list env keep any
    real codex login or ambient API key from flipping readiness.
    """
    tmp = tmp_path_factory.mktemp("codex_stub_host")
    host_home = tmp / "home"
    host_home.mkdir()
    workspace = tmp / "workspace"
    workspace.mkdir()
    stub_bin = tmp / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "codex"
    stub.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "codex-cli {_CODEX_STUB_VERSION}"; fi\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    host_name = f"codex-floor-{uuid.uuid4().hex[:8]}"

    env = {
        "PATH": os.pathsep.join([str(stub_bin), os.environ["PATH"]]),
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
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent",
                "host",
                "--server",
                live_server,
                "--non-interactive",
            ],
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
                f"stub-codex host never came online:\n{log_path.read_text()[-2000:]}"
            )
        yield {**row, "workspace": str(workspace)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_working_codex_above_capability_floor_is_not_outdated(
    live_server: str,
    codex_stub_host: dict[str, Any],
) -> None:
    """A capable codex install must not be flagged outdated on its host.

    Reproduces the reported journey end to end: the host's working codex is
    classified ``version-too-low``, so the composer marks Codex outdated and
    swaps it for another harness. Passes once the readiness map reports the
    CLI as ready or merely needing a codex login, and Codex stays selected.
    """
    _run_in_fresh_loop(_drive_codex_readiness(live_server, codex_stub_host))


async def _drive_codex_readiness(base_url: str, host: dict[str, Any]) -> None:
    agent_id = _codex_agent_id(base_url)
    notices_text: str | None = None
    selected_agent_label: str | None = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        try:
            # Land on the Codex agent and the stub host the way a returning
            # user does — via the persisted last pick and host choice.
            await page.add_init_script(
                f'window.localStorage.setItem("omnigent:last-agent-id", {json.dumps(agent_id)})'
            )
            await page.add_init_script(
                "window.localStorage.setItem("
                f'"omnigent:last-host-choice", {json.dumps(host["host_id"])})'
            )
            await page.add_init_script(
                "window.localStorage.setItem("
                '"omnigent:recent-workspaces", '
                f"JSON.stringify({json.dumps({host['host_id']: [host['workspace']]})}))"
            )
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            # Let the picker's readiness-driven harness resolution settle.
            await page.wait_for_timeout(4_000)
            notices = page.get_by_test_id("new-chat-landing-notices")
            with contextlib.suppress(Exception):
                notices_text = (await notices.inner_text()).strip()
            selector = page.get_by_test_id("new-chat-landing-agent-select")
            with contextlib.suppress(Exception):
                selected_agent_label = (await selector.first.inner_text()).strip()
            # Hold the settled composer state so a recording shows it legibly.
            await page.wait_for_timeout(2_000)
        finally:
            await page.close()
            await context.close()
            await browser.close()

    row = _fetch_host_row(base_url, host["name"])
    assert row is not None, "stub-codex host dropped offline mid-test"
    availability = (row.get("configured_harnesses") or {}).get("codex-native")
    assert availability != "version-too-low", (
        f"a working codex {_CODEX_STUB_VERSION} (>= its 0.129.0 policy-hook "
        "capability floor) must not be classified 'version-too-low'; the "
        f"composer showed notice {notices_text!r} and selected {selected_agent_label!r}"
    )
    assert availability in (True, "needs-auth"), (
        "codex-native on a host with a working codex CLI and no login must "
        f"read ready or 'needs-auth', got {availability!r}"
    )
    # The user-visible half: a readiness that reads ready/needs-auth must not
    # drive the composer's "Harness is outdated. Using <other> instead" swap.
    if notices_text is not None:
        assert "outdated" not in notices_text.lower(), (
            "composer must not mark Codex outdated / swap it for another "
            f"harness when its CLI is capable; it showed: {notices_text!r}"
        )
