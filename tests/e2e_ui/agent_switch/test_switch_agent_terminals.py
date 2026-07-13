"""E2E: an in-place agent switch prunes the old agent's terminals from the UI.

Regression coverage for the stale-terminal bug: switching a session's
agent closes the old agent's terminals on the runner (the switch route's
background ``POST /reset-state``), but the web UI's terminal list is
SSE-primary — before the runner published ``session.resource.deleted``
during the switch reset, the closed terminal stayed pinned in the
Terminals rail / main terminal view as a dead "Closed" tab whose attach
failed with "Bridge closed: terminal resource not found or not running".

The test drives the real user flow: launch a terminal in a session,
switch the agent through the header's Switch-agent dialog, and verify
the dead terminal disappears from the rail instead of lingering as a
"Closed" entry.
"""

from __future__ import annotations

import os
import re
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

# The header's "Switch agent" button was removed from the web UI; the
# SwitchAgentDialog component and the /switch-agent route still exist but
# have no UI entry point, so this browser-driven flow can't run. The env
# var is the opt-in to re-run once the affordance returns (and to drop
# this gate again).
pytestmark = pytest.mark.skipif(
    not os.environ.get("OMNIGENT_E2E_SWITCH_AGENT_UI"),
    reason="Switch-agent has no UI entry point (header button removed); "
    "set OMNIGENT_E2E_SWITCH_AGENT_UI=1 once the affordance returns.",
)

# Generous budget for the switch's background runner reset: the server
# queues it after the switch response, and the runner closes PTYs with
# a bounded per-terminal timeout before the deleted events publish.
_RESET_TIMEOUT_S = 60.0
_RESET_POLL_INTERVAL_S = 0.5


def _builtin_agent_id(base_url: str, name: str) -> str:
    """Look up a built-in agent's id by name via ``GET /v1/agents``.

    Only built-ins are bindable switch targets, and the Switch-agent
    dialog keys its option testids off the agent id, so the test needs
    the id to click the right option.

    :param base_url: Spawned server base URL, e.g.
        ``"http://127.0.0.1:51234"``.
    :param name: Built-in agent name, e.g. ``"hello_world"``.
    :returns: The agent id, e.g. ``"ag_abc123"``.
    """
    resp = httpx.get(f"{base_url}/v1/agents", timeout=10.0)
    resp.raise_for_status()
    agents = resp.json()["data"]
    matches = [a["id"] for a in agents if a["name"] == name]
    assert matches, (
        f"built-in agent {name!r} not listed in /v1/agents "
        f"(got {[a['name'] for a in agents]}) — the switch dialog would "
        f"have no target to offer."
    )
    return matches[0]


def test_switch_agent_prunes_dead_terminals(
    page: Page,
    terminal_session: tuple[str, str],
) -> None:
    """Switching the agent removes the old agent's terminal from the rail.

    Launches the agent-declared ``zsh`` terminal over REST (same runner
    path as ``sys_terminal_launch``, minus an LLM turn to flake), then
    switches the session to the ``hello_world`` built-in through the
    header dialog. The switch's runner-side reset closes the terminal;
    the UI must drop it — a lingering row (it renders with a "Closed"
    badge) means the ``session.resource.deleted`` events from the reset
    never reached the client and the bug has regressed.
    """
    base_url, session_id = terminal_session

    launch = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/terminals",
        json={"terminal": "zsh", "session_key": "main"},
        timeout=60.0,
    )
    launch.raise_for_status()

    page.goto(f"{base_url}/c/{session_id}")

    # Scope rail-content lookups to the desktop "Workspace" rail so they
    # don't match the hidden mobile drawer that mirrors the same testids.
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("Shells")).click()
    terminal_row = rail.get_by_role("button").filter(has_text="zsh").filter(has_text="main")
    expect(terminal_row.first).to_be_visible(timeout=30_000)

    # Switch in place to the hello_world built-in via the header dialog.
    # hello_world is an SDK harness, so it is always offered as a
    # history-preserving target regardless of the source agent.
    target_id = _builtin_agent_id(base_url, "hello_world")
    page.get_by_test_id("switch-agent-header").click()
    expect(page.get_by_test_id("switch-agent-dialog")).to_be_visible()
    page.get_by_test_id("switch-agent-select").click()
    page.get_by_test_id(f"switch-agent-option-{target_id}").click()
    page.get_by_test_id("switch-agent-submit").click()
    # The dialog closes only on a 2xx switch; a failure (e.g. 409 busy)
    # keeps it open with an inline error, which this would catch.
    expect(page.get_by_test_id("switch-agent-dialog")).to_have_count(0, timeout=30_000)

    # Wait for the switch's background runner reset to land before
    # asserting on the UI: the authoritative /terminals list goes empty
    # once the runner has closed the PTY. Without this gate the UI
    # assertion below could race the reset and pass against a
    # still-running terminal. Sleep-based polling mirrors the suite's
    # existing convention (see test_stale_stream.py) — the reset runs in
    # a server background task this process can't await directly.
    deadline = time.monotonic() + _RESET_TIMEOUT_S
    last_listing: object = "not polled yet"
    while time.monotonic() < deadline:
        resp = httpx.get(
            f"{base_url}/v1/sessions/{session_id}/resources/terminals",
            timeout=10.0,
        )
        if resp.status_code == 200:
            last_listing = resp.json().get("data")
            if last_listing == []:
                break
        else:
            last_listing = f"HTTP {resp.status_code}: {resp.text[:200]}"
        time.sleep(_RESET_POLL_INTERVAL_S)
    else:
        raise AssertionError(
            f"runner did not close the session's terminals within "
            f"{_RESET_TIMEOUT_S:.0f}s of the switch — /terminals still "
            f"returns {last_listing!r}; the reset-state teardown did not run."
        )

    # The dead terminal is pruned from the rail. A surviving row here is
    # the regression: the client cache kept the closed terminal because
    # no session.resource.deleted arrived (it would render with a
    # "Closed" badge and a dead xterm on click).
    expect(terminal_row).to_have_count(0, timeout=15_000)
    expect(rail.get_by_role("button").filter(has_text="Closed")).to_have_count(0)
