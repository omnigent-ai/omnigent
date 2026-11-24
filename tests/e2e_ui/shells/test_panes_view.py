"""E2E: the panes view behind ``workspace_sidebar_v2``.

With the flag on, a desktop owner's Terminal view is the panes view: the agent's terminal is
one pane, ⌘D splits it with a shell, the pane keys move between panes, ⌘K lists the pane
commands, and a pane's title strip has a menu that closes it. With a split shown, Chat keeps
the panes on screen and the web chat is the agent's pane. A shell tab in the workspace sidebar
splits the same way, and its split shows as one strip entry that closes whole. With the flag
off, the same session keeps the classic Terminal view.

The session is terminal-first (``omnigent.ui = terminal``), so the runner starts the agent's
own terminal, and its agent declares a ``zsh`` shell, running ``bash``, for splits. The flag is
turned on by answering ``/v1/info`` with it added, as ``sessions/test_workspace_sidebar_v2.py``
does, and the page reports a Mac, where the pane keys are bound. Input matrices and edge cases
are in the component tests (``useTerminalPaneHotkeys``, ``terminalPaneActions``,
``SplitPanesMainView``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, open_right_rail

_AGENT_NAME = "panes_demo"
_SHELL_OS_ENV = """
    os_env:
      type: caller_process
      cwd: .
      sandbox:
        type: none"""
_AGENT_YAML = f"""\
name: {_AGENT_NAME}
prompt: |
  You are a terminal panes test assistant. Reply with one short sentence.

executor:
  model: gpt-4o-mini
  harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none

terminals:
  zsh:
    command: bash
    args: ["--noprofile", "--norc"]{_SHELL_OS_ENV}
"""
# The agent's own terminal, which the runner starts for a terminal-first session.
_AGENT_PANE = '[data-terminal-pane^="terminal:terminal_tui"]'
_DESKTOP = {"width": 1440, "height": 900}
# The pane keys are the Mac's; elsewhere Ctrl+D and Ctrl+arrows are the shell's.
_REPORT_MAC = """
Object.defineProperty(navigator, "platform", { value: "MacIntel" });
Object.defineProperty(navigator, "userAgentData", { value: { platform: "macOS" } });
"""


@pytest.fixture
def panes_session(
    live_server: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[str, str]]:
    """A terminal-first session bound to the runner; the runner starts its agent terminal."""
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    bundle = bundle_files({f"{_AGENT_NAME}.yaml": _AGENT_YAML.encode()})
    created = post_session_bundle(httpx.post, f"{live_server}/v1/sessions", bundle, timeout=10.0)
    created.raise_for_status()
    session_id = created.json()["session_id"]
    try:
        bind_session_runner(
            httpx.patch, live_server, session_id, str(_server_state["runner_id"]), timeout=10.0
        )
        httpx.patch(
            f"{live_server}/v1/sessions/{session_id}",
            json={"labels": {"omnigent.ui": "terminal"}},
            timeout=10.0,
        ).raise_for_status()
        yield live_server, session_id
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            respawned_runner.wait(timeout=10)


def _set_flag(page: Page, enabled: bool) -> None:
    """Answer ``/v1/info`` with the server's own payload and the flag set to ``enabled``."""

    def answer(route: Route) -> None:
        response = route.fetch()
        info = response.json()
        info["features"] = {**(info.get("features") or {}), "workspace_sidebar_v2": enabled}
        route.fulfill(response=response, json=info)

    page.route("**/v1/info", answer)


def _open(page: Page, base_url: str, session_id: str, *, flag: bool = True) -> None:
    page.add_init_script(_REPORT_MAC)
    _set_flag(page, flag)
    page.set_viewport_size(_DESKTOP)
    page.goto(f"{base_url}/c/{session_id}")


def _select_view(page: Page, view: str) -> None:
    """Pick Chat or Terminal from the header's switch, once the agent's terminal makes it live."""
    segment = page.get_by_test_id(f"view-mode-{view}")
    expect(segment).to_be_enabled(timeout=60_000)
    segment.click()
    expect(segment).to_have_attribute("aria-pressed", "true")


def _connected(terminal: Locator) -> Locator:
    expect(terminal).to_be_visible(timeout=60_000)
    expect(terminal).to_have_attribute("data-state", "connected", timeout=60_000)
    return terminal.locator("textarea.xterm-helper-textarea")


def _main_panes(page: Page) -> Locator:
    panes = page.get_by_test_id("split-panes-main-view")
    expect(panes).to_be_visible(timeout=60_000)
    return panes


def _split_from(page: Page, panes: Locator, keyboard: Locator) -> Locator:
    """Split with ⌘D from ``keyboard``, and return the new pane's terminal input once it has it."""
    before = panes.get_by_test_id("terminal-view").count()
    keyboard.focus()
    page.keyboard.press("Meta+d")
    terminals = panes.get_by_test_id("terminal-view")
    expect(terminals).to_have_count(before + 1, timeout=60_000)
    return _connected(terminals.last)


def test_split_move_between_list_and_close_panes_in_the_terminal_view(
    page: Page, panes_session: tuple[str, str]
) -> None:
    """⌘D splits, ⌘⌥← and ⌘⌥→ move between panes, ⌘K lists Panes, and a strip's menu closes one."""
    base_url, session_id = panes_session
    _open(page, base_url, session_id)
    _select_view(page, "terminal")
    panes = _main_panes(page)
    agent = _connected(panes.locator(_AGENT_PANE).get_by_test_id("terminal-view"))

    shell = _split_from(page, panes, agent)
    expect(shell).to_be_focused(timeout=30_000)
    page.keyboard.press("Meta+Alt+ArrowLeft")
    expect(agent).to_be_focused()

    page.keyboard.press("Meta+k")
    palette = page.get_by_test_id("command-palette-input")
    expect(palette).to_be_focused(timeout=10_000)
    # Stands in for "Move Pane…", which a real split doesn't list yet; check for it once it does.
    palette.fill("Swap with Pane Right")
    # Escape waits for the search to settle on the row, as a person reading it would.
    swap = page.get_by_role("option", name=re.compile("Swap with Pane Right"))
    expect(swap).to_have_attribute("aria-selected", "true")
    page.keyboard.press("Escape")
    expect(palette).to_have_count(0)
    agent.focus()

    page.keyboard.press("Meta+Alt+ArrowRight")
    expect(shell).to_be_focused()
    shell_strip = panes.get_by_role("toolbar", name=re.compile(r"^zsh · .* pane$"))
    shell_strip.click(button="right")
    page.get_by_role("menuitem", name="Close Pane").click()
    page.get_by_role("button", name="Close shell").click()
    expect(panes.get_by_test_id("terminal-view")).to_have_count(1, timeout=30_000)
    expect(panes.locator(_AGENT_PANE)).to_be_visible()


def test_chat_keeps_a_split_on_screen_and_splits_from_its_composer(
    page: Page, panes_session: tuple[str, str]
) -> None:
    """In Chat, a split stays on screen with the chat as the agent's pane, and ⌘D splits."""
    base_url, session_id = panes_session
    _open(page, base_url, session_id)
    _select_view(page, "terminal")
    panes = _main_panes(page)
    agent = _connected(panes.locator(_AGENT_PANE).get_by_test_id("terminal-view"))
    _split_from(page, panes, agent)

    _select_view(page, "chat")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible(timeout=30_000)
    # The chat renders in the agent's pane, beside the split's shell.
    in_pane = panes.locator("[data-chat-pane-body]").get_by_placeholder("Send a message…")
    expect(in_pane).to_be_visible(timeout=30_000)
    expect(panes.get_by_role("toolbar", name=re.compile(r"^zsh · .* pane$"))).to_have_count(1)

    composer.focus()
    page.keyboard.press("Meta+d")
    expect(panes.get_by_role("toolbar", name=re.compile(r"^zsh · .* pane$"))).to_have_count(
        2, timeout=60_000
    )


def test_a_tabs_sidebar_shell_splits_keeps_both_shells_through_a_reload_and_closes_whole(
    page: Page, panes_session: tuple[str, str]
) -> None:
    """A sidebar shell tab splits with ⌘D, keeps both shells over a reload, and closes as one."""
    base_url, session_id = panes_session
    _open(page, base_url, session_id)
    open_right_rail(page)
    sidebar = page.get_by_role("complementary", name="Workspace")
    expect(sidebar.get_by_role("group", name="Workspace history")).to_be_visible(timeout=60_000)
    sidebar.get_by_role("button", name="New tab", exact=True).click()
    expect(sidebar.get_by_role("combobox", name="Open an app in this tab")).to_be_focused(
        timeout=10_000
    )
    sidebar.get_by_role("option", name=re.compile(r"^Terminal", re.IGNORECASE)).click()
    first = _connected(sidebar.get_by_test_id("terminal-view").last)

    second = _split_from(page, sidebar, first)
    page.keyboard.type("echo second")
    page.keyboard.press("Enter")
    # The split shows as one strip entry, named for its focused shell.
    entry = sidebar.get_by_role("tab", name=re.compile(r"^zsh · .* \+1"))
    expect(entry).to_have_count(1, timeout=30_000)
    expect(second).to_be_focused()

    page.reload()
    open_right_rail(page)
    expect(sidebar.get_by_test_id("terminal-view")).to_have_count(2, timeout=60_000)
    for index in range(2):
        expect(sidebar.get_by_test_id("terminal-view").nth(index)).to_have_attribute(
            "data-state", "connected", timeout=60_000
        )

    sidebar.get_by_role("button", name=re.compile(r"^Close zsh · .* \+1$")).click()
    page.get_by_role("button", name="Close shells").click()
    expect(sidebar.get_by_test_id("terminal-view")).to_have_count(0, timeout=30_000)
    expect(sidebar.get_by_role("tab", name=re.compile(r"^zsh · "))).to_have_count(0)


def test_flag_off_keeps_the_classic_terminal_view(
    page: Page, panes_session: tuple[str, str]
) -> None:
    """With ``-workspace_sidebar_v2`` the same session shows the classic Terminal view."""
    base_url, session_id = panes_session
    _open(page, base_url, session_id, flag=False)
    _select_view(page, "terminal")
    _connected(page.locator(".main-terminal-view").get_by_test_id("terminal-view").first)
    expect(page.get_by_test_id("split-panes-main-view")).to_have_count(0)
    expect(page.locator("[data-pane-strip]")).to_have_count(0)
