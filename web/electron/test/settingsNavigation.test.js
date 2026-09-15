const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  SETTINGS_ACCELERATOR,
  SETTINGS_PATH,
  focusedConnectedWindow,
  aboutMenuItem,
  macApplicationMenu,
  settingsMenuItem,
} = require("../src/settingsNavigation");

function fakeWindow(url, destroyed = false) {
  return {
    isDestroyed: () => destroyed,
    webContents: { getURL: () => url },
  };
}

describe("Settings native menu item", () => {
  it("uses the platform-native Command/Ctrl-comma accelerator", () => {
    let opened = 0;
    const item = settingsMenuItem(() => {
      opened += 1;
    });

    assert.equal(SETTINGS_PATH, "/settings");
    assert.equal(SETTINGS_ACCELERATOR, "CmdOrCtrl+,");
    assert.equal(item.id, "open_settings");
    assert.equal(item.label, "Settings…");
    assert.equal(item.accelerator, SETTINGS_ACCELERATOR);
    item.click();
    assert.equal(opened, 1);
  });

  it("opens the custom About window and keeps Settings in the macOS app menu", () => {
    let opened = 0;
    const about = aboutMenuItem("Omnigent", () => {
      opened += 1;
    });
    const settings = settingsMenuItem(() => {});
    const menu = macApplicationMenu("Electron", about, settings);

    assert.equal(menu.label, "Electron");
    assert.equal(menu.submenu[0], about);
    assert.equal(about.id, "open_about");
    assert.equal(about.label, "About Omnigent");
    about.click();
    assert.equal(opened, 1);
    assert.equal(menu.submenu[2], settings);
    assert.deepEqual(
      menu.submenu.filter((entry) => entry.role).map((entry) => entry.role),
      ["services", "hide", "hideOthers", "unhide", "quit"],
    );
  });
});

describe("focusedConnectedWindow", () => {
  it("targets the focused connected window, not another server window", () => {
    const first = fakeWindow("https://one.example/app/c/first");
    const focused = fakeWindow("https://two.example/base/c/second");
    const windows = new Map([
      [first, { origin: "https://one.example", serverUrl: "https://one.example/app" }],
      [focused, { origin: "https://two.example", serverUrl: "https://two.example/base" }],
    ]);

    assert.equal(focusedConnectedWindow(focused, windows), focused);
  });

  it("rejects setup, foreign-login, popup, destroyed, and invalid windows", () => {
    const setup = fakeWindow("file:///setup/index.html");
    const away = fakeWindow("https://login.example/sso");
    const destroyed = fakeWindow("https://server.example/app", true);
    const invalid = fakeWindow("not a URL");
    const popup = fakeWindow("https://server.example/oauth");
    const windows = new Map([
      [setup, { origin: null, serverUrl: null }],
      [away, { origin: "https://server.example", serverUrl: "https://server.example/app" }],
      [destroyed, { origin: "https://server.example", serverUrl: "https://server.example/app" }],
      [invalid, { origin: "https://server.example", serverUrl: "https://server.example/app" }],
    ]);

    assert.equal(focusedConnectedWindow(null, windows), null);
    assert.equal(focusedConnectedWindow(setup, windows), null);
    assert.equal(focusedConnectedWindow(away, windows), null);
    assert.equal(focusedConnectedWindow(popup, windows), null);
    assert.equal(focusedConnectedWindow(destroyed, windows), null);
    assert.equal(focusedConnectedWindow(invalid, windows), null);
  });
});
