"""Rows of the desktop-only Settings sections are catalog rows: a link opens one at its control.

Settings search sends a chosen result to `/settings/<section>#<row>`, and so does any link to a
row. These sections render only in the desktop app, so a stand-in for its bridge is injected
before the app loads, as `test_desktop_update.py` does.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.desktop.test_desktop_update import _DEFAULT_CONFIG, _install_update_stub

# The desktop bridge's base methods as guarded no-ops, plus a CLI the user pointed at a path of
# their own, which Local CLI offers to reset.
_DESKTOP_SHELL = """
(() => {
  const cli = %s;
  window.omnigentDesktop = {
    kind: "electron",
    setBadgeCount: function () {},
    notify: function () { return Promise.resolve(false); },
    onNotificationActivated: function () { return function () {}; },
    getServerPicker: function () { return Promise.resolve(null); },
    switchServer: function () { return Promise.resolve(); },
    openServerSetup: function () {},
    getCliStatus: function () { return Promise.resolve(cli); },
    resetCliPath: function () { return Promise.resolve(cli); },
  };
})();
"""

_CUSTOM_PATH_CLI = {
    "installed": True,
    "path": "/opt/tools/omnigent",
    "version": "0.22.0",
    "source": "configured",
}


@pytest.mark.parametrize(
    "fragment",
    ["local-cli", "local-cli:reset"],
    ids=["row", "reset-part"],
)
def test_a_link_to_the_cli_row_opens_local_cli_at_its_reset_button(
    page: Page, live_server: str, fragment: str
) -> None:
    page.add_init_script(_DESKTOP_SHELL % json.dumps(_CUSTOM_PATH_CLI))
    page.goto(f"{live_server}/settings/cli#{fragment}")

    row = page.locator('[data-settings-item="local-cli"]')
    expect(row.get_by_text("/opt/tools/omnigent")).to_be_visible(timeout=30_000)
    expect(row.get_by_role("button", name="Reset to auto-detected")).to_be_focused(timeout=10_000)


@pytest.mark.parametrize(
    ("fragment", "control"),
    [
        ("update-mode", ("combobox", "Update mode")),
        ("install-updates-on-quit", ("switch", "Install downloaded updates on next quit")),
        ("check-for-updates", ("button", "Check for updates now")),
    ],
    ids=["mode", "install-on-quit", "check"],
)
def test_a_link_to_an_updates_row_opens_updates_at_its_control(
    page: Page, live_server: str, fragment: str, control: tuple[str, str]
) -> None:
    _install_update_stub(page, '{ state: "idle" }', _DEFAULT_CONFIG)
    page.goto(f"{live_server}/settings/updates#{fragment}")

    role, name = control
    target = page.locator(f'[data-settings-item="{fragment}"]').get_by_role(role, name=name)
    expect(target).to_be_focused(timeout=30_000)
