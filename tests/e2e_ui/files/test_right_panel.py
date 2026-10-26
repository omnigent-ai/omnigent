"""E2E: right rail shell soft tab and file viewer.

Execution-logs coverage was dropped: that surface used to be a
``SessionRail`` card but the rail is now tabbed (Files/Changes/Agents)
and the only entry point left is the ``md:hidden`` mobile session menu —
unreachable on the desktop viewport these tests run at.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _TERMINAL_PANEL_FILE,
    _TERMINAL_PANEL_FILE_CONTENT,
    configure_mock_llm,
    open_right_rail,
)


@pytest.mark.parametrize(
    ("tab_name", "tooltip", "shortcut", "expected_state"),
    [
        ("Files", "Files", "1", "active"),
        ("Agents", "Agents", "4", "inactive"),
    ],
)
def test_workspace_tab_hover_tooltip(
    page: Page,
    terminal_session: tuple[str, str],
    tab_name: str,
    tooltip: str,
    shortcut: str,
    expected_state: str,
) -> None:
    """Explain fixed workspace tabs on hover without changing selection."""
    base_url, session_id = terminal_session
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)

    rail = page.get_by_role("complementary", name="Workspace")
    tab = rail.get_by_role("tab", name=re.compile(f"^{tab_name}"))

    expect(tab).to_have_attribute("data-state", expected_state)
    tab.hover()
    tip = page.get_by_role("tooltip")
    expect(tip).to_contain_text(tooltip)
    keycaps = tip.locator('[data-slot="kbd"]')
    expect(keycaps).to_have_count(4)
    expect(keycaps.nth(0)).to_have_text("⌘" if sys.platform == "darwin" else "Ctrl")
    expect(keycaps.nth(1)).to_have_text("⌥" if sys.platform == "darwin" else "Alt")
    expect(keycaps.nth(2)).to_have_text("]")
    expect(keycaps.nth(3)).to_have_text(shortcut)
    expect(tab).to_have_attribute("data-state", expected_state)


@pytest.fixture
def no_seeded_file_leftovers(terminal_session: tuple[str, str]) -> Iterator[None]:
    """Fail at teardown if the journey's cleanup missed a copy of the seed file.

    The filesystem API writes it under the listing's ``base``; the scripted
    terminal (``cwd: .``) writes a copy into the runner's cwd, i.e. pytest's.
    """
    base_url, session_id = terminal_session
    listing = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem",
        timeout=10.0,
    )
    listing.raise_for_status()
    seeded_files = (
        Path(listing.json()["base"]) / _TERMINAL_PANEL_FILE,
        Path.cwd() / _TERMINAL_PANEL_FILE,
    )
    yield
    leftovers = [path for path in seeded_files if path.exists()]
    for path in leftovers:
        path.unlink()
    assert not leftovers, f"seeded files left behind: {leftovers}"


@pytest.mark.usefixtures("no_seeded_file_leftovers")
def test_right_panel_terminals_and_file_viewer(
    page: Page,
    terminal_session: tuple[str, str],
) -> None:
    """Launch a terminal and exercise the shell soft tab + file viewer.

    The terminal session fixture registers the test agent and creates a
    session bound to it. We send ``spin up zsh``, then verify:

    - the agent-launched shell appears as a closable soft tab in the rail,
      its xterm connects inline when the tab is opened, and closing the tab
      (after the confirm) kills the terminal;
    - the Files tab can open a workspace file and the file viewer renders
      its contents.

    The file is seeded via the filesystem API rather than relying on the
    agent's terminal ``printf`` to write it: the agent's workspace cwd
    differs by environment (empty on CI), and the changed-files panel only
    refreshes on turn active→idle, so a late PTY write races the refresh.
    The terminal launch above is the real terminal coverage; the file
    viewer just needs a deterministic file present in the scanned workspace.
    """
    base_url, session_id = terminal_session
    listing = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem",
        timeout=10.0,
    )
    listing.raise_for_status()
    # Both places the journey writes the seed; see ``no_seeded_file_leftovers``.
    workspace_file = Path(listing.json()["base"]) / _TERMINAL_PANEL_FILE
    test_file = Path.cwd() / _TERMINAL_PANEL_FILE
    if test_file.exists():
        test_file.unlink()

    # Seed the file into the session workspace (same API the markdown tests
    # use) so it deterministically appears in the Files panel.
    seed_resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_TERMINAL_PANEL_FILE}",
        json={"content": f"{_TERMINAL_PANEL_FILE_CONTENT}\n", "encoding": "utf-8"},
        timeout=10.0,
    )
    seed_resp.raise_for_status()

    # Scope rail-content lookups to the desktop "Workspace" rail so they
    # don't match the hidden mobile drawer that mirrors the same testids.
    rail = page.get_by_role("complementary", name="Workspace")

    try:
        page.goto(f"{base_url}/c/{session_id}")
        # The rail defaults open but is remembered per session; ensure it is
        # open so the shell tab and Files panel below are reachable.
        open_right_rail(page)

        composer = page.get_by_placeholder("Send a message…")
        expect(composer).to_be_visible()
        composer.fill("spin up zsh")
        page.get_by_role("button", name="Send", exact=True).click()

        # The agent's sys_terminal_launch creates a shell that appears directly
        # as a closable soft tab in the rail's strip (shells are 1:1 with the
        # session's terminals — there is no separate Shells tab or list). The
        # launch rides an LLM turn, so allow main's generous 60s. The tab's
        # accessible name starts with its label "zsh · main"; the "Close …" x is
        # a separate button, so anchor the regex to disambiguate.
        shell_tab = rail.get_by_role("button", name=re.compile(r"^zsh · main"))
        expect(shell_tab).to_be_visible(timeout=60_000)

        # Agent-spawned shells don't steal focus, so click the tab to open it;
        # its xterm renders inside the rail's content slot and the chat page is
        # left undisturbed (no main-column takeover). Assert no VISIBLE main
        # terminal surface: terminal-first sessions keep a hidden pre-warmed
        # surface mounted (data-visible="false"), which is not a takeover.
        shell_tab.click()
        terminal_view = rail.get_by_test_id("terminal-view")
        expect(terminal_view.last).to_be_visible(timeout=20_000)
        expect(terminal_view.last).to_have_attribute("data-state", "connected", timeout=20_000)
        expect(
            page.locator('[data-testid="main-terminal-view"][data-visible="true"]')
        ).to_have_count(0)
        # Closing the tab kills the terminal, so its "x" confirms first; after
        # confirming the xterm unmounts and the rail returns to its default
        # (files) view for the Files steps below.
        rail.get_by_role("button", name="Close zsh · main", exact=True).click()
        page.get_by_role("button", name="Close shell").click()
        expect(terminal_view).to_have_count(0)

        # Switch to the Files tab and open the seeded file. The
        # changed-file row renders two buttons carrying the filename: the
        # file-open button (visible text) and an icon-only Download button
        # (aria-label "Download <name>"). Filter to the open button by its
        # visible text so the locator stays single-element under strict mode.
        rail.get_by_role("tab", name=re.compile("Files")).click()
        file_button = rail.get_by_role(
            "button", name=re.compile(re.escape(_TERMINAL_PANEL_FILE))
        ).filter(has_text=_TERMINAL_PANEL_FILE)
        expect(file_button).to_be_visible(timeout=60_000)
        file_button.click()

        # Two FileViewer instances mount (hidden mobile drawer + desktop
        # rail); scope to the rail so the locator is single-element and the
        # visible desktop instance — the bare-page ``.last`` can resolve to
        # the hidden drawer once other push panels have mounted.
        file_viewer = rail.get_by_test_id("file-viewer")
        expect(file_viewer).to_be_visible()
        # The open file is identified by its tab (the desktop viewer header no
        # longer repeats a top-level filename — it's redundant with the tab).
        # The tab's close button carries an "Close <name>" label.
        expect(
            rail.get_by_role("button", name=f"Close {_TERMINAL_PANEL_FILE}", exact=True)
        ).to_be_visible()
        # The viewer renders the file body; scope inside the file viewer and
        # pick the first match — its presence proves the content landed.
        expect(file_viewer.get_by_text(_TERMINAL_PANEL_FILE_CONTENT).first).to_be_visible(
            timeout=20_000
        )
    finally:
        workspace_file.unlink(missing_ok=True)
        if test_file.exists():
            test_file.unlink()


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_agent_opens_workspace_file(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
    request: pytest.FixtureRequest,
    width: int,
) -> None:
    """A real tool dispatch opens the existing viewer through the session stream."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": width, "height": 900})
    filesystem = f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem"
    listing = page.request.get(filesystem)
    assert listing.status == 200, listing.text()
    name = f"ui-open-{session_id}.md"
    target = Path(listing.json()["base"]) / name
    target.write_text("# Agent opened this file\n\nWorkspace preview.\n")
    request.addfinalizer(lambda: target.unlink(missing_ok=True))
    prompt = f"Show my file {session_id}"
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "open_file",
                        "name": "ui_open_file",
                        "arguments": json.dumps({"path": name}),
                    }
                ]
            },
            {"text": "Requested the file preview."},
        ],
        match=prompt,
        required_tools=["ui_open_file"],
    )
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(prompt)
    page.get_by_role("button", name="Send", exact=True).click()
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(viewer).to_be_visible(timeout=60_000)
    expect(viewer.get_by_text("Agent opened this file", exact=True)).to_be_visible()
    expect(page).to_have_url(re.compile(r"[?&]file=" + re.escape(name)))


@pytest.mark.parametrize("file_name", ["big-download.txt", "big-download.md"])
def test_file_viewer_download_saves_the_complete_file(
    page: Page,
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
    tmp_path: Path,
    file_name: str,
) -> None:
    """The viewer's "Download file" saves the whole file, past the read cap.

    The viewer's JSON read truncates at the server's cap, and the download
    used to reuse it, so anything larger came back cut off with no
    warning. Open files one byte over the cap from the Files tree, download
    through the real server and runner, and compare every byte. Single-line
    Markdown must also render its read-only prefix without a parser overflow.
    """
    from omnigent.runner.environment_filesystem import _MAX_READ_BYTES

    base_url, session_id = seeded_session
    # Listing the root materializes a fresh session's workspace on the
    # runner; on CI it does not exist on disk before that.
    listing = page.request.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem"
    )
    assert listing.status == 200, listing.text()
    target = Path(listing.json()["base"]) / file_name
    line = b"0123456789abcdef" * 4 + b"\n"
    payload = (
        b"y" * (_MAX_READ_BYTES + 1)
        if file_name.endswith(".md")
        else (line * (_MAX_READ_BYTES // len(line) + 1))[: _MAX_READ_BYTES + 1]
    )
    target.write_bytes(payload)
    request.addfinalizer(lambda: target.unlink(missing_ok=True))

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    # The row's open button carries the name as visible text; the icon-only
    # copy-path button next to it carries it only in its label.
    row = rail.get_by_role("button", name=re.compile(re.escape(file_name))).filter(
        has_text=file_name
    )
    expect(row).to_be_visible(timeout=30_000)
    row.click()
    viewer = rail.get_by_test_id("file-viewer")
    expect(viewer).to_be_visible()
    if file_name.endswith(".md"):
        editor = viewer.locator(".monaco-editor")
        expect(editor).to_be_visible(timeout=30_000)
        editor.locator(".view-lines").click()
        page.keyboard.type("x")
        expect(editor.get_by_text("Cannot edit in read-only editor")).to_be_visible()
        page.keyboard.press("Escape")
        expect(viewer.locator(".tiptap")).to_have_count(0)
        expect(viewer.get_by_text("too large to load fully", exact=False)).to_be_visible()
    rail.get_by_role("button", name="View settings").click()
    with page.expect_download() as download_info:
        page.get_by_role("menuitem", name="Download file").click()
    download = download_info.value
    assert download.suggested_filename == file_name
    saved = tmp_path / file_name
    download.save_as(saved)
    assert saved.read_bytes() == payload
