"""Back, Forward and Settings keys follow what the desktop shell's menu routes to the page.

A shell that lists the Go menu's commands owns ⌘[ / ⌘], and a shell with the command bridge owns
Settings' chord. An older shell, or a bridge from before the list, leaves them to the page. Each
case injects a stand-in for that shell's bridge before the app loads.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.desktop._mac_shell import MAC_SHELL_INIT_SCRIPT

# The command bridge on top of the base shell; `%s` is the commands its menu routes, or null.
_COMMAND_BRIDGE = """
(() => {
  const routed = %s;
  window.__menuCommand = null;
  Object.assign(window.omnigentDesktop, {
    onDesktopCommand(callback) {
      window.__menuCommand = (command) => callback({ command, browserViewId: null });
      return () => {};
    },
    setDesktopCommandState() {},
    ...(routed ? { desktopCommands: Object.freeze(routed) } : {}),
  });
})();
"""

_GO_COMMANDS = '["close", "go-back", "go-forward", "quit"]'
_SETTINGS_SECTION = re.compile(r"/settings/[a-z-]+$")


def _open_settings_from_home(page: Page, base_url: str) -> None:
    """Open the home page, then Settings in-app, so Back has a page to return to."""
    page.goto(f"{base_url}/")
    expect(page.get_by_role("heading", name="What should we build?")).to_be_visible(timeout=30_000)
    page.get_by_role("link", name="Settings", exact=True).click()
    # /settings redirects to its first section; wait for that, so a key never races it.
    expect(page).to_have_url(_SETTINGS_SECTION, timeout=10_000)


@pytest.mark.parametrize(
    "bridge",
    [None, _COMMAND_BRIDGE % "null"],
    ids=["shell-without-command-bridge", "bridge-without-command-list"],
)
def test_an_older_shell_leaves_back_and_forward_to_the_page(
    page: Page, live_server: str, bridge: str | None
) -> None:
    page.add_init_script(MAC_SHELL_INIT_SCRIPT)
    if bridge:
        page.add_init_script(bridge)
    _open_settings_from_home(page, live_server)

    page.keyboard.press("Meta+BracketLeft")
    expect(page).to_have_url(f"{live_server}/", timeout=10_000)
    page.keyboard.press("Meta+BracketRight")
    expect(page).to_have_url(_SETTINGS_SECTION, timeout=10_000)


def test_a_shell_that_lists_the_go_menu_owns_back_and_forward(
    page: Page, live_server: str
) -> None:
    page.add_init_script(MAC_SHELL_INIT_SCRIPT)
    page.add_init_script(_COMMAND_BRIDGE % _GO_COMMANDS)
    _open_settings_from_home(page, live_server)
    settings_url = page.url

    # The menu's accelerator takes ⌘[, so the page leaves the key alone.
    page.keyboard.press("Meta+BracketLeft")
    page.wait_for_timeout(300)
    expect(page).to_have_url(settings_url)
    # The menu's Go Back reaches the page as a command.
    page.evaluate("window.__menuCommand('go-back')")
    expect(page).to_have_url(f"{live_server}/", timeout=10_000)


def test_settings_chord_is_the_pages_only_without_the_command_bridge(
    page: Page, live_server: str
) -> None:
    page.add_init_script(MAC_SHELL_INIT_SCRIPT)
    page.goto(f"{live_server}/")
    expect(page.get_by_role("heading", name="What should we build?")).to_be_visible(timeout=30_000)
    page.keyboard.press("Meta+Alt+Comma")
    expect(page).to_have_url(_SETTINGS_SECTION, timeout=10_000)


def test_the_command_bridge_leaves_settings_to_its_menu(page: Page, live_server: str) -> None:
    page.add_init_script(MAC_SHELL_INIT_SCRIPT)
    page.add_init_script(_COMMAND_BRIDGE % _GO_COMMANDS)
    page.goto(f"{live_server}/")
    expect(page.get_by_role("heading", name="What should we build?")).to_be_visible(timeout=30_000)
    page.keyboard.press("Meta+Alt+Comma")
    page.wait_for_timeout(300)
    expect(page).to_have_url(f"{live_server}/")
