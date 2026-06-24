"""E2E: responsive Web UI workflow at a phone-sized viewport.

``AppShell.test.tsx`` already covers the mobile session-menu state
machine in jsdom — but jsdom does not apply the Tailwind ``md:`` media
queries, so it cannot prove the responsive *layout* contract. These
tests run a real browser at a 390×844 (phone) viewport and assert the
behaviors only real CSS can produce:

  - the desktop workspace rail (``md:flex``) is collapsed off-screen;
  - the session-menu FAB (``md:hidden``) is the navigation entry point;
  - the desktop right-panel toggle (``hidden md:inline-flex``) is gone;
  - each rail surface (Changes/Files/Agents) is reachable through the
    FAB and opens a full-screen drawer;
  - the chat composer is usable and streams a response on a phone.

Files are seeded via the filesystem PUT endpoint so the navigation
tests are deterministic (no LLM); the final chat test exercises the
real streaming path the same way ``test_smoke`` does.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, ViewportSize, expect

# iPhone-12-class portrait viewport — comfortably below the Tailwind
# ``md`` breakpoint (768px) so every ``md:`` rule resolves to its
# mobile branch.
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

_SEEDED_FILE_PATH = "mobile_workflow.txt"
_SEEDED_FILE_CONTENT = "File body opened through the mobile Changes drawer."

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def mobile_session_with_file(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    """Seed one file and clean up the per-session workdir on teardown.

    The agent spec's ``os_env.cwd: .`` makes filesystem PUTs land in
    ``<repo-root>/<session_id>/``; remove it after the test so no
    untracked files are left behind.
    """
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_SEEDED_FILE_PATH}",
        json={"content": _SEEDED_FILE_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def test_mobile_collapses_rail_and_surfaces_fab(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """At a phone width the rail is hidden and the FAB replaces it.

    Proves the responsive layout swap that jsdom can't: the desktop
    workspace ``aside`` (``hidden md:flex``) must not be visible, the
    session-menu FAB (``md:hidden``) must be, and the desktop-only
    right-panel collapse toggle (``hidden md:inline-flex``) must be
    absent from the viewport.
    """
    base_url, session_id = seeded_session
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")

    # The composer renders, confirming the chat surface is the primary
    # mobile view (no rail competing for space).
    expect(page.get_by_placeholder("Ask the agent anything…")).to_be_visible()

    # Desktop rail is in the DOM but collapsed off-screen (hidden md:flex).
    # A DOM-scoped locator makes not_to_be_visible() genuinely assert
    # display:none rather than passing on a 0-match accessibility locator.
    expect(page.locator('aside[aria-label="Workspace"]')).not_to_be_visible()

    # FAB present; desktop right-panel toggle present-but-hidden.
    expect(page.get_by_role("button", name="Open session menu")).to_be_visible()
    expect(
        page.locator(
            'button[aria-label="Collapse right panel"], button[aria-label="Expand right panel"]'
        )
    ).not_to_be_visible()


@pytest.fixture
def mobile_session_with_child_agent(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    """Seed one sub-agent (child) session under the parent.

    The Agents surface is gated on ``childSessions.length > 0`` — the
    root having at least one child (so "main + child" is genuinely
    multi-agent). A real spawn needs an LLM run, which is slow and
    non-deterministic; instead we create the child directly via the
    JSON ``POST /v1/sessions`` contract with ``parent_session_id`` set,
    reusing the parent's bound ``agent_id``. That makes the server
    record a ``kind="sub_agent"`` conversation, which
    ``GET /sessions/{id}/child_sessions`` then lists even with no
    tasks. Cleaned up by deleting the child on teardown.
    """
    base_url, session_id = seeded_session
    parent = httpx.get(
        f"{base_url}/v1/sessions/{session_id}",
        timeout=10.0,
    )
    parent.raise_for_status()
    agent_id = parent.json()["agent_id"]

    child = httpx.post(
        f"{base_url}/v1/sessions",
        json={
            "agent_id": agent_id,
            "parent_session_id": session_id,
            "sub_agent_name": "researcher",
            "title": "researcher:auth",
        },
        timeout=30.0,
    )
    child.raise_for_status()
    # JSON POST /v1/sessions returns a SessionResponse (id), unlike the
    # multipart bundled create used by seeded_session (session_id).
    child_id = child.json()["id"]
    try:
        yield (base_url, session_id)
    finally:
        httpx.delete(
            f"{base_url}/v1/sessions/{child_id}",
            timeout=10.0,
        )


def test_mobile_fab_lists_file_surfaces_and_omits_absent_ones(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The FAB menu degrades gracefully to only the available surfaces.

    A plain ``hello_world`` session has a workspace (os_env), no child
    agents, and is not claude-native — so the menu must list Files (the
    single files surface; its Changed/All scope is an in-panel toggle)
    and Agents (unconditional — the panel lists at least the main
    agent, badge "1"), and must NOT list Shells or Tasks. The session's
    only terminal is the auto-created embedded Omnigent REPL, which is
    plumbing for the Chat/Terminal pill, not inventory — listing it
    read as a phantom "main" terminal on agents that don't run a TUI,
    so ``inventoryTerminals`` excludes it from the FAB/rail.
    """
    base_url, session_id = seeded_session
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")

    page.get_by_role("button", name="Open session menu").click()

    expect(page.get_by_role("menuitem", name="Files", exact=True)).to_be_visible()
    # Agents is always present; "1" = just the main agent. "0" means
    # the main agent was dropped from the count.
    expect(page.get_by_role("menuitem", name=re.compile(r"Agents\s*1"))).to_be_visible()
    # No Shells entry: the embedded REPL terminal is excluded from
    # the inventory (reachable via the Chat/Terminal pill instead). An
    # entry here means the REPL leaked back into the FAB gating.
    expect(page.get_by_role("menuitem", name="Shells")).to_have_count(0)
    # No separate Changes entry (merged into Files) and no todos.
    expect(page.get_by_role("menuitem", name="Changes")).to_have_count(0)
    expect(page.get_by_role("menuitem", name="Tasks")).to_have_count(0)


def test_mobile_fab_shows_agents_entry_when_child_agents_exist(
    page: Page,
    mobile_session_with_child_agent: tuple[str, str],
) -> None:
    """The Agents entry appears once the session has a child agent.

    Counterpart to the omits-absent-ones test: with one sub-agent
    seeded under the parent, the multi-agent gate
    (``childSessions.length > 0``) is satisfied, so the FAB now lists
    Agents alongside Files. Selecting it opens the agents drawer.
    """
    base_url, session_id = mobile_session_with_child_agent
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")

    page.get_by_role("button", name="Open session menu").click()

    agents_entry = page.get_by_role("menuitem", name="Agents")
    # 10s budget: the rail fetches child_sessions on load, so the entry
    # can take a fetch cycle (plus cold-start latency) to appear.
    expect(agents_entry).to_be_visible(timeout=10_000)
    expect(page.get_by_role("menuitem", name="Files", exact=True)).to_be_visible()

    # Selecting Agents opens the agents (sub-agents) drawer.
    agents_entry.click()
    drawer = page.get_by_test_id("subagents-panel-drawer")
    expect(drawer).to_have_attribute("data-state", "open")


def test_mobile_files_drawer_opens_seeded_file(
    page: Page,
    mobile_session_with_file: tuple[str, str],
) -> None:
    """Open the Files drawer from the FAB and view the seeded file.

    The full chain a phone user hits: FAB → Files → full-screen files
    drawer → tap a file → full-screen file viewer with real content.
    Uses the Files (folder-tree) surface rather than Changes: the
    session workspace isn't a git repo, so the changed-files diff is
    empty, but the directory listing always includes the seeded file.
    Failure means the mobile drawer routing or the mobile FileViewer
    push-panel regressed.
    """
    base_url, session_id = mobile_session_with_file
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")

    page.get_by_role("button", name="Open session menu").click()
    page.get_by_role("menuitem", name="Files", exact=True).click()

    drawer = page.get_by_test_id("files-panel-drawer")
    expect(drawer).to_have_attribute("data-state", "open")

    # The PUT-seeded file appears in the working-folder directory listing.
    # Anchor the name to the filename so the row button is matched, not the
    # sibling "Download <file>" action button. 30s budget: the panel polls
    # the workspace listing endpoint on an interval, so the seeded file can
    # take a poll cycle (plus cold-start latency) to surface.
    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(_SEEDED_FILE_PATH)}"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    # Selecting a file closes the drawer and opens the mobile FileViewer
    # push-panel. ``:visible`` targets the on-screen instance (the desktop
    # inline viewer carries the same testid but is display:none here).
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_contain_text(_SEEDED_FILE_PATH)
    expect(file_viewer.get_by_text(_SEEDED_FILE_CONTENT).first).to_be_visible(timeout=20_000)


# The agent turn is dispatched to the in-process harness, which
# occasionally produces no assistant output on the first turn (the
# runner goes idle after dispatch — a nondeterministic harness
# scheduling stall, not a real-LLM artifact since this drives the mock
# LLM). Rerun on failure rather than widen the already-generous 60s
# wait, which a stalled turn would never satisfy.
@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_mobile_chat_send_and_response(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The composer streams an assistant reply at a phone viewport.

    Mirrors ``test_smoke`` but at 390px wide, confirming the composer
    and message list stay usable on a phone (no rail stealing layout).
    """
    base_url, session_id = seeded_session
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder("Ask the agent anything…")
    expect(composer).to_be_visible()
    composer.fill("Say 'pong' in one word.")
    page.get_by_role("button", name="Send", exact=True).click()

    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_be_visible(timeout=60_000)
    expect(assistant).to_have_text(re.compile(r"\S"), timeout=60_000)
