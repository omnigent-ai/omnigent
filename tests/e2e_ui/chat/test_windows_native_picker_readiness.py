"""E2E: a Windows host's native agents must not read as ready in the picker.

The reported journey, from a Windows-only host: the host daemon finds the
``claude`` CLI installed and logged in, so its hello frame reports
``configured_harnesses: {"claude-native": True, ...}`` (built by
``configured_harness_map()`` — sent from ``omnigent/host/connect.py``). That
probe is binary+login only and has **no platform gate**, while the runner
refuses every native-terminal launch on Windows by design ("Native ...
terminal (tmux/PTY) is not supported on Windows",
``omnigent/runner/native/orchestration.py``). The New Chat picker trusts the
readiness map, so on a Windows host the "Claude Code" row lists inline as
ready-to-use with no warning badge — and selecting it always fails with
``native_terminal_start_failed``.

These tests compute the readiness signal with the REAL
``configured_harness_map()`` (the daemon's own code) under a simulated
Windows platform, serve that live value through the ``/v1/hosts`` wire shape,
and drive the rendered landing picker:

* ``test_windows_host_native_agent_is_flagged_in_picker`` asserts the FIXED
  behavior — on a host that cannot launch it, the Claude Code row must carry
  the amber ``new-chat-landing-agent-warning-*`` badge (the picker's existing
  marker for a harness the host can't run). It FAILS on the unfixed build:
  the readiness map says ``True`` on Windows, so the row renders inline with
  no badge. A fix that instead *removes* the row should update the assertion
  here (the row-visibility anchor) rather than the intent.
* ``test_posix_host_native_agent_stays_ready`` pins the control: the same
  installed+logged-in CLI on a POSIX host keeps the row inline and unbadged,
  so a fix cannot over-gate Linux/macOS.
* ``test_missing_cli_native_agent_shows_setup_badge`` pins the ticket's part
  B at the point of choice: when the host's readiness map DOES say the
  harness can't run (here: CLI not installed), the picker badges the row.
  This passes today — the join landed after the report — and guards the
  mechanism a platform gate must flow through.

Platform seam: a host-side fix is expected to consult
``omnigent._platform.IS_WINDOWS`` (the flag the runner's refusal already
uses). :func:`_force_windows_platform` patches that flag on
``omnigent._platform`` and — ``raising=False`` — on the readiness/install
modules, so a ``from omnigent._platform import IS_WINDOWS`` binding in either
is covered too. A fix gating on a different predicate should extend that
helper, not weaken the assertions.

Why the ``page.route`` stubbing and the async-in-a-fresh-thread shape: both
are inherited from ``chat/test_hide_unconfigured_harnesses.py`` — the e2e
harness's runner tunnels into the server and registers no *host*, so faking
``/v1/hosts`` (with ``configured_harnesses``) and ``/v1/agents`` is the
established way to drive the landing picker, and once a pytest-playwright
sync test has run in the session, pytest-asyncio can't start a loop on the
main thread. Crucially, the faked ``configured_harnesses`` VALUE is not a
literal: it is computed live by the daemon's own readiness code under the
simulated platform, so a host-side fix flows into this test without editing
it.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Route, async_playwright, expect

import omnigent._platform as _platform
import omnigent.onboarding.harness_install as hi
import omnigent.onboarding.harness_readiness as hr
from omnigent.harness_availability import HarnessAvailability

# Stubbed host the composer auto-selects (the tunneled runner registers no
# host). Keyed identically in the recent-workspaces localStorage seed.
_HOST_ID = "host_windows_native"
_HOST_NAME = "windows-box"

# The exemplar native-terminal agent from the report ("the most obvious
# entry"). Name/harness mirror the seeded ``claude-native-ui`` builtin so the
# picker classifies it as a fully-supported native coding agent (inline row).
_AGENT_ID = "ag_claude_native_win"

# Hold on the picker's decisive state long enough for a session recording to
# show it (the drives themselves settle in well under a second).
_RECORDING_HOLD_MS = 1_500


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* to completion in a dedicated thread with its own event loop.

    The e2e_ui suite runs many pytest-playwright **sync** tests in the same
    session; once one has run, pytest-asyncio can't start a loop on the main
    thread. Running the coroutine from a fresh thread via :func:`asyncio.run`
    sidesteps that. Any exception (including assertion failures) is captured
    and re-raised on the calling thread so the test fails normally.

    :param coro: The coroutine to run to completion.
    :raises Exception: Whatever the coroutine raised, re-raised here.
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


def _claude_cli_installed_and_logged_in(mp: pytest.MonkeyPatch, config_home: Path) -> None:
    """Make the readiness probe see an installed, in-range, logged-in claude CLI.

    Mirrors the reported Windows host's state (claude.exe on PATH, signed in)
    using the stubbing conventions of ``tests/onboarding/test_harness_readiness.py``:
    ``shutil.which`` finds every CLI, ``--version`` probes answer in-range, and
    the CLI login probe reports signed-in. The config home is pointed at an
    empty directory so a developer's/CI's real provider config can't flip the
    verdict.

    :param mp: The active monkeypatch context.
    :param config_home: Empty directory to use as ``OMNIGENT_CONFIG_HOME``.
    """
    mp.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    mp.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")
    mp.setattr(_platform, "_cli_fallback_dirs", lambda: ())

    def _stub_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        """Answer CLI probes without executing anything.

        ``--version`` probes get an in-range version per family (OpenCode's
        window is narrow; Cursor/Hermes use calendar versions); anything else
        reports failure so an unexpected probe reads "not ready" rather than
        executing a real binary.
        """
        if len(argv) >= 2 and argv[1] == "--version":
            if argv[0].endswith("opencode"):
                version = "1.17.7\n"
            elif argv[0].endswith("cursor-agent") or argv[0].endswith("hermes"):
                version = "2026.07.01\n"
            else:
                version = "9.9.9\n"
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=version, stderr="")
        return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="")

    mp.setattr(hi.subprocess, "run", _stub_run)
    mp.setattr(hi, "harness_cli_logged_in", lambda _key, **_kw: True)


def _no_clis_installed(mp: pytest.MonkeyPatch, config_home: Path) -> None:
    """Make the readiness probe see no harness CLI installed at all.

    The state part B of the report is about: the host genuinely cannot run
    the native harness, and the readiness map says so (``claude-native``
    reads ``"binary-missing"``).

    :param mp: The active monkeypatch context.
    :param config_home: Empty directory to use as ``OMNIGENT_CONFIG_HOME``.
    """
    mp.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    mp.setattr(hi.shutil, "which", lambda name: None)
    mp.setattr(_platform, "_cli_fallback_dirs", lambda: ())
    mp.setattr(hi, "harness_cli_logged_in", lambda _key, **_kw: False)


def _force_windows_platform(mp: pytest.MonkeyPatch) -> None:
    """Simulate running the host daemon's readiness code on Windows.

    A real Windows host is unobtainable in this harness, so the platform
    flags are forced instead — the one precondition of the reported journey
    that cannot be established for real here. Patches the canonical flag on
    ``omnigent._platform`` plus (``raising=False``) any module-level bindings
    a fix may import into the readiness/install modules.

    :param mp: The active monkeypatch context.
    """
    mp.setattr(_platform, "IS_WINDOWS", True)
    mp.setattr(_platform, "IS_POSIX", False, raising=False)
    mp.setattr(hr, "IS_WINDOWS", True, raising=False)
    mp.setattr(hi, "IS_WINDOWS", True, raising=False)


def _live_claude_native_readiness(
    monkeypatch: pytest.MonkeyPatch,
    config_home: Path,
    *,
    windows: bool,
    cli_installed: bool = True,
) -> HarnessAvailability:
    """Compute claude-native readiness exactly as the host daemon would.

    Calls the real :func:`configured_harness_map` — the function whose result
    the daemon sends in its hello frame — with the CLI environment stubbed,
    optionally under a simulated Windows platform. The stubs live in a
    :func:`pytest.MonkeyPatch.context` so they are reverted before Playwright
    launches any subprocess.

    :param monkeypatch: The test's monkeypatch fixture.
    :param config_home: Empty directory to use as ``OMNIGENT_CONFIG_HOME``.
    :param windows: Whether to simulate the Windows platform.
    :param cli_installed: Whether the claude CLI reads installed + signed in.
    :returns: The readiness value for ``"claude-native"``, e.g. ``True``.
    """
    with monkeypatch.context() as mp:
        if cli_installed:
            _claude_cli_installed_and_logged_in(mp, config_home)
        else:
            _no_clis_installed(mp, config_home)
        if windows:
            _force_windows_platform(mp)
        return hr.configured_harness_map()["claude-native"]


def _hosts_body(readiness: HarnessAvailability) -> str:
    """Stub body for ``GET /v1/hosts``: one online host the composer picks.

    :param readiness: The live-computed ``claude-native`` readiness to report,
        mirroring the wire shape the ``host.hello`` readiness map produces.
    :returns: JSON body for the route stub.
    """
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": _HOST_NAME,
                    "owner": "e2e",
                    "status": "online",
                    "configured_harnesses": {"claude-native": readiness},
                }
            ]
        }
    )


def _agents_body() -> str:
    """Stub body for ``GET /v1/agents``: the Claude Code native agent."""
    return json.dumps(
        {
            "data": [
                {
                    "id": _AGENT_ID,
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent",
                    "harness": "claude-native",
                    "skills": [],
                }
            ]
        }
    )


async def _register_routes(page: Any, readiness: HarnessAvailability) -> None:
    """Register the host/agent stubs and neutralize agent discovery.

    :param page: The Playwright page to install routes on.
    :param readiness: The live-computed ``claude-native`` readiness to serve.
    """

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(
            status=200, content_type="application/json", body=_hosts_body(readiness)
        )

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    async def handle_agent_scan(route: Route) -> None:
        # Neutralize agent discovery so only the stubbed agent feeds the
        # picker; sessions other tests left behind would otherwise leak in and
        # swap the selection out from under the assertions.
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        )

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)


async def _open_landing_picker(page: Any, base_url: str) -> None:
    """Load the landing composer and open the agent/harness picker.

    :param page: The Playwright page (routes already registered).
    :param base_url: The live server's base URL.
    """
    # Seed a recent working directory so the composer auto-fills (it never
    # has to touch the host-less file browser).
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )
    await page.goto(f"{base_url}/")
    await page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)
    await page.get_by_test_id("new-chat-landing-agent-select").click()


async def _drive_windows(base_url: str, readiness: HarnessAvailability) -> None:
    """Assert the picker flags Claude Code as unavailable on the Windows host.

    :param base_url: The live server's base URL.
    :param readiness: The live-computed readiness served via ``/v1/hosts``.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_routes(page, readiness)
            await _open_landing_picker(page, base_url)
            row = page.get_by_test_id(f"new-chat-landing-agent-{_AGENT_ID}")
            badge = page.get_by_test_id(f"new-chat-landing-agent-warning-{_AGENT_ID}")
            # Claude Code is a fully-supported native coding agent, so its row
            # lists inline in the "Harnesses" group (badged or not).
            await expect(row).to_be_visible(timeout=30_000)
            # Hold the decisive picker state on screen for the recording.
            await page.wait_for_timeout(_RECORDING_HOLD_MS)
            # FIXED behavior: on a host that can never launch a native
            # terminal, the row must carry the picker's unavailability badge.
            # UNFIXED: the daemon reported readiness True from Windows, so the
            # row renders as ready-to-use — no badge — and selecting it fails
            # with native_terminal_start_failed.
            assert await badge.count() > 0, (
                f"claude-native readiness computed {readiness!r} on a "
                "simulated Windows host; the New Chat picker offers 'Claude Code' "
                "inline with no unavailability badge, but every native-terminal "
                "launch on Windows fails with native_terminal_start_failed "
                "(tmux/PTY is not supported on Windows)."
            )
        finally:
            # Close the context before the browser: a video finalizes on
            # context close, so this keeps footage even from a failing drive.
            await page.context.close()
            await browser.close()


async def _drive_posix(base_url: str, readiness: HarnessAvailability) -> None:
    """Assert the picker keeps Claude Code ready and unbadged on a POSIX host.

    :param base_url: The live server's base URL.
    :param readiness: The live-computed readiness served via ``/v1/hosts``.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_routes(page, readiness)
            await _open_landing_picker(page, base_url)
            row = page.get_by_test_id(f"new-chat-landing-agent-{_AGENT_ID}")
            badge = page.get_by_test_id(f"new-chat-landing-agent-warning-{_AGENT_ID}")
            await expect(row).to_be_visible(timeout=30_000)
            assert await badge.count() == 0, (
                f"claude-native readiness computed {readiness!r} on a POSIX host "
                "with the CLI installed and logged in; the picker must keep the "
                "Claude Code row ready (no unavailability badge) — a Windows gate "
                "must not leak onto platforms that can launch native terminals."
            )
        finally:
            # Close the context before the browser: a video finalizes on
            # context close, so this keeps footage even from a failing drive.
            await page.context.close()
            await browser.close()


async def _drive_badged(base_url: str, readiness: HarnessAvailability) -> None:
    """Assert the picker badges Claude Code when the host says it can't run.

    :param base_url: The live server's base URL.
    :param readiness: The live-computed readiness served via ``/v1/hosts``.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_routes(page, readiness)
            await _open_landing_picker(page, base_url)
            row = page.get_by_test_id(f"new-chat-landing-agent-{_AGENT_ID}")
            badge = page.get_by_test_id(f"new-chat-landing-agent-warning-{_AGENT_ID}")
            await expect(row).to_be_visible(timeout=30_000)
            await expect(badge).to_be_visible()
            # Hold the badged row on screen for the recording.
            await page.wait_for_timeout(_RECORDING_HOLD_MS)
        finally:
            # Close the context before the browser: a video finalizes on
            # context close, so this keeps footage even from a failing drive.
            await page.context.close()
            await browser.close()


def test_windows_host_native_agent_is_flagged_in_picker(
    seeded_session: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A Windows host's claude-native must not be offered as ready in the picker.

    Reproduces the report: the daemon-computed readiness (CLI installed and
    logged in, platform Windows) flows through ``/v1/hosts`` into the landing
    picker, which must flag the Claude Code row as unavailable. Fails on the
    unfixed build, where the readiness map has no platform gate.
    """
    base_url, session_id = seeded_session
    del session_id  # this flow never creates a session — only reads the picker
    readiness = _live_claude_native_readiness(monkeypatch, tmp_path, windows=True)
    _run_in_fresh_loop(_drive_windows(base_url, readiness))


def test_posix_host_native_agent_stays_ready(
    seeded_session: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A POSIX host with a configured claude CLI keeps Claude Code ready.

    The control for the Windows gate: the same installed+logged-in CLI on the
    real (POSIX) platform must keep reading ready end to end, so the Windows
    gate cannot over-gate the platforms that CAN launch native terminals.
    """
    base_url, session_id = seeded_session
    del session_id  # this flow never creates a session — only reads the picker
    readiness = _live_claude_native_readiness(monkeypatch, tmp_path, windows=False)
    assert readiness is True, (
        f"precondition: an installed+logged-in claude CLI must read ready on "
        f"POSIX, got {readiness!r} — the CLI stubs no longer satisfy the probe"
    )
    _run_in_fresh_loop(_drive_posix(base_url, readiness))


def test_missing_cli_native_agent_shows_setup_badge(
    seeded_session: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A host whose readiness map says "can't run" gets a badge in the picker.

    Pins the ticket's part B at the point of choice: the daemon-computed
    signal for a host without the CLI (``"binary-missing"``) flows through
    ``/v1/hosts`` into the picker, which marks the Claude Code row. This is
    the join a Windows platform gate must flow through for part A's fix to be
    user-visible.
    """
    base_url, session_id = seeded_session
    del session_id  # this flow never creates a session — only reads the picker
    readiness = _live_claude_native_readiness(
        monkeypatch, tmp_path, windows=False, cli_installed=False
    )
    assert readiness is not True, (
        f"precondition: with no CLI installed, claude-native must not read "
        f"ready, got {readiness!r} — the CLI-absence stubs no longer hold"
    )
    _run_in_fresh_loop(_drive_badged(base_url, readiness))
