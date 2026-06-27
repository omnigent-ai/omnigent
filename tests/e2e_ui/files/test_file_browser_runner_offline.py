"""E2E: file browser degrades gracefully when the runner is offline.

Companion to issue #386 — "File browser shows 'No files in workspace'
after the host is reconnected to the server and before you send a new
message (because runner is not created yet)".

The fix routes the workspace queries to the **host-filesystem API** when
the runner is known offline but the host itself is online, so the tree
isn't empty after a host reconnect and before the first message. That
fallback only engages for *host-bound* sessions (the host daemon is a
separate online entity that can serve ``GET /v1/hosts/{id}/filesystem``
without a runner). The host-fallback mapping logic is covered
unit-level by ``ap-web/src/hooks/useWorkspaceChangedFiles.test.tsx``
("host-filesystem fallback" suites) — the e2e_ui fixture spawns a
*direct* (tunnel-bound) runner with no managed host, so it cannot
reproduce the host-reconnect scenario end-to-end.

What this test *can* cover in a real browser is the adjacent code path:
the file browser hooks must handle the runner-offline state without
crashing or surfacing a raw "Failed to load" error. After killing the
runner and reloading the page (the reconnect analog — session history
loads, runner not yet re-initialized), the Files panel must still render
a graceful state. This guards the precondition the host-fallback builds
on (the offline branch in ``useWorkspaceAllFiles`` /
``useWorkspaceDirectory``) against regressing into an unhandled error.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

# A deterministic file seeded into the workspace so the Files tab has
# content to show while the runner is online (proving the panel works
# before we take the runner offline).
_SEED_FILE = "runner_offline_probe.md"
_SEED_CONTENT = "# probe\nrunner-offline file browser smoke test\n"


def _find_runner_pids() -> list[int]:
    """Find PIDs running the runner entry point (``omnigent.runner._entry``)."""
    result = subprocess.run(
        ["pgrep", "-f", "omnigent.runner._entry"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [int(line.strip()) for line in result.stdout.strip().splitlines() if line.strip()]


def test_file_browser_handles_runner_offline(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """File browser renders a graceful state after the runner is killed + reloaded.

    Seeds a file, verifies it appears in the Files tab while the runner is
    online, kills the runner, waits for ``runner_online: false``, then
    reloads (the post-reconnect analog where history loads but the runner
    is not yet re-initialized). Asserts the Files panel still renders and
    does NOT show a raw "Failed to load" error — the offline branch must
    degrade gracefully rather than surfacing an unhandled failure.
    """
    live_server, session_id = seeded_session

    # Seed the file into the session workspace so the Files tab has a
    # visible entry while the runner is online.
    seed_resp = httpx.put(
        f"{live_server}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_SEED_FILE}",
        json={"content": f"{_SEED_CONTENT}\n", "encoding": "utf-8"},
        timeout=10.0,
    )
    seed_resp.raise_for_status()

    page.goto(f"{live_server}/c/{session_id}")
    open_right_rail(page)

    composer = page.get_by_placeholder("Ask the agent anything…")
    expect(composer).to_be_visible()

    # Match the suite's convention: a single generous timeout for the
    # file-browser assertions below (slow CI turns + cold loads).
    expect.set_options(timeout=15_000)

    rail = page.get_by_role("complementary", name="Workspace")
    # The Files tab is present by default. Switch to it and wait for the
    # seeded file to appear — proves the file browser works online first.
    rail.get_by_role("tab", name=re.compile("Files")).click()
    expect(rail.get_by_text(re.compile(re.escape(_SEED_FILE)))).to_be_visible()

    # Verify the health endpoint reports online before the kill.
    health_before = httpx.get(
        f"{live_server}/health?session_id={session_id}",
        timeout=5,
    ).json()
    assert health_before.get("session", {}).get("runner_online") is True, (
        f"runner_online should be true before kill, got: {health_before}"
    )

    # Kill the runner (sibling of the server, not a child).
    runner_pids = _find_runner_pids()
    assert runner_pids, "No runner processes found to kill"
    for pid in runner_pids:
        os.kill(pid, signal.SIGKILL)

    # Poll until the health endpoint reports the runner offline.
    health_after: dict[str, object] = {}
    for _attempt in range(10):
        time.sleep(0.5)
        health_after = httpx.get(
            f"{live_server}/health?session_id={session_id}",
            timeout=5,
        ).json()
        if health_after.get("session", {}).get("runner_online") is False:
            break
    assert health_after.get("session", {}).get("runner_online") is False, (
        f"runner_online should be false after kill, got: {health_after}"
    )

    # Reload the page — the reconnect analog: session history loads from
    # the server, but the runner is not yet re-initialized (no message has
    # been sent). This is exactly the state the #386 fix targets; here the
    # fixture's session isn't host-bound so the host-fallback doesn't
    # engage, but the offline branch must still degrade gracefully.
    page.reload()
    expect(page).to_have_url(re.compile(rf"/c/{re.escape(session_id)}"), timeout=15_000)
    expect(composer).to_be_visible(timeout=15_000)

    # Re-open the rail (its open state is per-conversation and may collapse
    # after reload) and switch back to the Files tab.
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("Files")).click(timeout=15_000)

    # The panel must render a graceful offline state — NOT a raw "Failed to
    # load" error. The host-fallback would show the tree for a host-bound
    # session; this direct-runner fixture shows the empty state, which is
    # the correct non-host degradation. Either way, no unhandled error.
    expect(page.get_by_text(re.compile("Failed to load"))).to_have_count(0)
    # The Files panel body is still present (the tab content rendered).
    expect(rail).to_be_visible()
